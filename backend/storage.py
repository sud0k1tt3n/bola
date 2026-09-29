"""
Хранилище на sqlite3 (стандартная библиотека, без ORM): проекты, снимки,
найденные участки, вердикты оператора и настройки интерфейса.

Файл базы — config.DB_PATH (backend/data/aeroroad.db). Сложные поля
(полигоны, параметры геопривязки) хранятся JSON-строками.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS photos (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    name TEXT NOT NULL,
    size_bytes INTEGER,
    width INTEGER,
    height INTEGER,
    status TEXT NOT NULL,          -- done | need_geo | failed
    geo_source TEXT,               -- exif | manual | none
    lat REAL,
    lon REAL,
    place TEXT,
    processing_ms INTEGER,
    overall_status TEXT,
    footprint TEXT,                -- GeoJSON Polygon
    georef TEXT,                   -- georef-блок ответа /api/analyze
    model_mode TEXT,
    pending_path TEXT,             -- исходный файл, пока ждёт ручной привязки
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS zones (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    photo_id TEXT NOT NULL,
    num INTEGER NOT NULL,
    cls TEXT NOT NULL,
    level TEXT NOT NULL,
    confidence REAL,
    bbox_px TEXT,
    polygon TEXT,                  -- кольцо [[lon, lat], ...]
    center TEXT,                   -- [lon, lat]
    area_m2 REAL,
    crop_url TEXT,
    place TEXT,
    verdict TEXT,                  -- confirmed | false_positive | NULL
    comment TEXT,
    detected_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_photos_project ON photos(project_id);
CREATE INDEX IF NOT EXISTS idx_zones_project ON zones(project_id);
CREATE INDEX IF NOT EXISTS idx_zones_photo ON zones(photo_id);
"""

# Настройки интерфейса (порог отображения и т.п.). Пороги детектора — в config.py.
DEFAULT_SETTINGS = {
    "minConfidence": 0.35,
    "alerts": {"blocked": True, "nogeo": True},
}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _loads(v: Optional[str]) -> Any:
    return json.loads(v) if v else None


class Storage:
    def __init__(self, db_path: Path = None):
        db_path = Path(db_path or config.DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # FastAPI выполняет sync-эндпоинты в пуле потоков → одно соединение под замком.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall()
            self._conn.commit()
            return rows

    # --- проекты ---------------------------------------------------------------
    def list_projects(self) -> list[dict]:
        rows = self._q(
            "SELECT p.*, (SELECT COUNT(*) FROM photos f WHERE f.project_id = p.id) AS photo_count, "
            "(SELECT COUNT(*) FROM zones z WHERE z.project_id = p.id) AS zone_count "
            "FROM projects p ORDER BY created_at DESC"
        )
        return [{"id": r["id"], "name": r["name"], "createdAt": r["created_at"],
                 "photoCount": r["photo_count"], "zoneCount": r["zone_count"]} for r in rows]

    def get_project(self, pid: str) -> Optional[dict]:
        return next((p for p in self.list_projects() if p["id"] == pid), None)

    def create_project(self, name: str) -> dict:
        pid = "pr-" + uuid.uuid4().hex[:8]
        self._q("INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)", (pid, name.strip(), now()))
        return self.get_project(pid)

    def delete_project(self, pid: str) -> bool:
        exists = self._q("SELECT 1 FROM projects WHERE id = ?", (pid,))
        self._q("DELETE FROM zones WHERE project_id = ?", (pid,))
        self._q("DELETE FROM photos WHERE project_id = ?", (pid,))
        self._q("DELETE FROM projects WHERE id = ?", (pid,))
        return bool(exists)

    def ensure_default_project(self) -> None:
        if not self._q("SELECT 1 FROM projects LIMIT 1"):
            self.create_project("Мой проект")

    # --- снимки ---------------------------------------------------------------
    def upsert_photo(self, **f) -> None:
        f.setdefault("updated_at", now())
        f.setdefault("created_at", f["updated_at"])
        for k in ("footprint", "georef"):
            if k in f and not isinstance(f[k], (str, type(None))):
                f[k] = json.dumps(f[k], ensure_ascii=False)
        cols = ", ".join(f)
        marks = ", ".join("?" for _ in f)
        updates = ", ".join(f"{k}=excluded.{k}" for k in f if k not in ("id", "created_at"))
        self._q(f"INSERT INTO photos ({cols}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}",
                tuple(f.values()))

    def get_photo(self, photo_id: str) -> Optional[sqlite3.Row]:
        rows = self._q("SELECT * FROM photos WHERE id = ?", (photo_id,))
        return rows[0] if rows else None

    def list_photos(self, project_id: Optional[str] = None) -> list[sqlite3.Row]:
        if project_id:
            return self._q("SELECT * FROM photos WHERE project_id = ? ORDER BY created_at DESC, rowid DESC", (project_id,))
        return self._q("SELECT * FROM photos ORDER BY created_at DESC, rowid DESC")

    def delete_photo(self, photo_id: str) -> None:
        self._q("DELETE FROM zones WHERE photo_id = ?", (photo_id,))
        self._q("DELETE FROM photos WHERE id = ?", (photo_id,))

    # --- участки ----------------------------------------------------------------
    def replace_zones(self, photo_id: str, project_id: Optional[str], zones: list[dict]) -> None:
        self._q("DELETE FROM zones WHERE photo_id = ?", (photo_id,))
        ts = now()
        for i, z in enumerate(zones, 1):
            self._q(
                "INSERT INTO zones (id, project_id, photo_id, num, cls, level, confidence, bbox_px, polygon, "
                "center, area_m2, crop_url, place, detected_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"{photo_id[:8]}-{i}", project_id, photo_id, i, z["class"], z["severity"], z["confidence"],
                 json.dumps(z["bbox_px"]), json.dumps(z["polygon_wgs84"]["coordinates"][0]),
                 json.dumps(z["center_wgs84"]), z["area_m2"], z.get("crop_url"), z.get("place"), ts, ts),
            )

    def list_zones(self, project_id: Optional[str] = None) -> list[sqlite3.Row]:
        sql = ("SELECT z.*, p.name AS photo_name FROM zones z LEFT JOIN photos p ON p.id = z.photo_id "
               "{} ORDER BY p.created_at DESC, z.num")
        if project_id:
            return self._q(sql.format("WHERE z.project_id = ?"), (project_id,))
        return self._q(sql.format(""))

    def get_zone(self, zone_id: str) -> Optional[sqlite3.Row]:
        rows = self._q("SELECT z.*, p.name AS photo_name FROM zones z LEFT JOIN photos p ON p.id = z.photo_id "
                       "WHERE z.id = ?", (zone_id,))
        return rows[0] if rows else None

    def set_verdict(self, zone_id: str, verdict: str, comment: Optional[str]) -> None:
        self._q("UPDATE zones SET verdict = ?, comment = ?, updated_at = ? WHERE id = ?",
                (verdict, comment, now(), zone_id))

    # --- настройки --------------------------------------------------------------
    def get_settings(self) -> dict:
        rows = self._q("SELECT value FROM kv WHERE key = 'settings'")
        saved = json.loads(rows[0]["value"]) if rows else {}
        out = json.loads(json.dumps(DEFAULT_SETTINGS))
        out.update({k: v for k, v in saved.items() if k != "alerts"})
        out["alerts"].update(saved.get("alerts", {}))
        return out

    def patch_settings(self, patch: dict) -> dict:
        cur = self.get_settings()
        if "minConfidence" in patch:
            mc = float(patch["minConfidence"])
            cur["minConfidence"] = min(max(mc / 100 if mc > 1 else mc, 0.0), 1.0)
        if isinstance(patch.get("alerts"), dict):
            cur["alerts"].update({k: bool(v) for k, v in patch["alerts"].items()})
        self._q("INSERT INTO kv (key, value) VALUES ('settings', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(cur),))
        return cur
