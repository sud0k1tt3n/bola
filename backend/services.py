"""
Прикладной слой интерфейса оператора: сохраняет результат анализа в
хранилище и собирает DTO для экранов «Снимки», «Участки», «Карта».

Сама детекция и геопривязка — в pipeline.py / detector.py / georef.py и
здесь не меняются: этот модуль только оборачивает analyze_image().
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional

import config
import settlements
from detector import HazardDetector
from pipeline import AnalyzeError, analyze_image
from storage import Storage, _loads

log = logging.getLogger(__name__)


def _place_for(lat: float, lon: float, manual: Optional[str]) -> str:
    if manual and manual.strip():
        return manual.strip()
    return settlements.describe(lat, lon) or f"{lat:.4f} N, {lon:.4f} E"


def _save_result(store: Storage, res: dict, *, name: str, size: int, project_id: Optional[str],
                 place: Optional[str], geo_source: str, created_at: Optional[str] = None) -> None:
    lon, lat = res["georef"]["center"]
    for d in res["detections"]:
        d["place"] = _place_for(d["center_wgs84"][1], d["center_wgs84"][0], place)
    fields = dict(
        id=res["image_id"], project_id=project_id, name=name, size_bytes=size,
        width=res["image_width"], height=res["image_height"], status="done",
        geo_source=geo_source, lat=lat, lon=lon, place=_place_for(lat, lon, place),
        processing_ms=res["processing_ms"], overall_status=res["overall_status"],
        footprint=res["footprint"], georef=res["georef"], model_mode=res["model"]["mode"],
        pending_path=None, error=None,
    )
    if created_at:
        fields["created_at"] = created_at
    store.upsert_photo(**fields)
    store.replace_zones(res["image_id"], project_id, res["detections"])


def process_upload(store: Storage, detector: HazardDetector, raw: bytes, filename: str,
                   project_id: Optional[str] = None, place: Optional[str] = None, **manual) -> dict:
    """
    Анализ нового снимка + запись в проект. Если координат нет (422) — файл
    откладывается со статусом need_geo, а в ошибку добавляется photo_id,
    чтобы оператор мог привязать его вручную (POST /api/photos/{id}/geo).
    """
    try:
        res = analyze_image(raw, detector, **manual)
    except AnalyzeError as exc:
        if exc.status == 422 and "lat" in exc.missing:
            photo_id = str(uuid.uuid4())
            config.PENDING_DIR.mkdir(parents=True, exist_ok=True)
            path = config.PENDING_DIR / photo_id
            path.write_bytes(raw)
            store.upsert_photo(id=photo_id, project_id=project_id, name=filename, size_bytes=len(raw),
                               status="need_geo", geo_source="none", place=place,
                               pending_path=str(path), error=exc.detail)
            exc.extra = {"photo_id": photo_id}
        raise
    geo_source = {"manual": "manual", "demo": "demo"}.get(res["georef"]["sources"].get("position"), "exif")
    _save_result(store, res, name=filename, size=len(raw), project_id=project_id,
                 place=place, geo_source=geo_source)
    res["photo"] = photo_dto(store.get_photo(res["image_id"]), store)
    res["zones"] = [zone_dto(z) for z in store.list_zones() if z["photo_id"] == res["image_id"]]
    return res


def apply_manual_geo(store: Storage, detector: HazardDetector, photo_id: str,
                     place: Optional[str] = None, **manual) -> dict:
    """Повторный прогон отложенного (или уже обработанного) снимка с ручными координатами."""
    row = store.get_photo(photo_id)
    if row is None:
        raise AnalyzeError(404, "Снимок не найден")
    if row["pending_path"] and Path(row["pending_path"]).exists():
        raw = Path(row["pending_path"]).read_bytes()
    else:
        src = config.UPLOADS_DIR / f"{photo_id}.jpg"
        if not src.exists():
            raise AnalyzeError(410, "Исходный файл снимка не сохранился — загрузите его заново")
        raw = src.read_bytes()
    res = analyze_image(raw, detector, image_id=photo_id, **manual)
    _save_result(store, res, name=row["name"], size=row["size_bytes"] or len(raw),
                 project_id=row["project_id"], place=place or row["place"], geo_source="manual",
                 created_at=row["created_at"])
    if row["pending_path"]:
        Path(row["pending_path"]).unlink(missing_ok=True)
    res["photo"] = photo_dto(store.get_photo(photo_id), store)
    res["zones"] = [zone_dto(z) for z in store.list_zones() if z["photo_id"] == photo_id]
    return res


# ---------------------------------------------------------------------------
# DTO (camelCase — формат фронтенда)
# ---------------------------------------------------------------------------
def photo_dto(r, store: Optional[Storage] = None) -> dict:
    georef = _loads(r["georef"]) or {}
    zones = [z for z in store.list_zones(r["project_id"]) if z["photo_id"] == r["id"]] if store else []
    done = r["status"] == "done"
    return {
        "id": r["id"],
        "projectId": r["project_id"],
        "name": r["name"],
        "sizeBytes": r["size_bytes"],
        "width": r["width"],
        "height": r["height"],
        "status": r["status"],
        "geoSource": r["geo_source"],
        "lat": r["lat"],
        "lon": r["lon"],
        "place": r["place"],
        "processingMs": r["processing_ms"],
        "overallStatus": r["overall_status"],
        "url": f"/static/uploads/{r['id']}.jpg" if done else None,
        "thumbUrl": f"/static/uploads/{r['id']}_thumb.jpg" if done else None,
        "footprint": _loads(r["footprint"]),
        "corners": georef.get("overlay_corners"),
        "georef": georef or None,
        "modelMode": r["model_mode"],
        "zoneTypes": sorted({z["cls"] for z in zones}),
        "zoneIds": [z["id"] for z in zones],
        "error": r["error"],
        "createdAt": r["created_at"],
    }


def zone_dto(z) -> dict:
    center = _loads(z["center"])
    return {
        "id": z["id"],
        "num": z["num"],
        "projectId": z["project_id"],
        "photoId": z["photo_id"],
        "photoName": z["photo_name"],
        "type": z["cls"],
        "label": config.CLASS_LABELS_RU.get(z["cls"], z["cls"]),
        "level": z["level"],
        "confidence": z["confidence"],
        "bboxPx": _loads(z["bbox_px"]),
        "polygon": _loads(z["polygon"]),
        "centroid": {"lon": center[0], "lat": center[1]} if center else None,
        "areaM2": z["area_m2"],
        "cropUrl": z["crop_url"],
        "photoUrl": f"/static/uploads/{z['photo_id']}.jpg",
        "place": z["place"],
        "verdict": z["verdict"],
        "comment": z["comment"],
        "detectedAt": z["detected_at"],
        "updatedAt": z["updated_at"],
    }


def list_photos(store: Storage, project_id: Optional[str]) -> list[dict]:
    zones = store.list_zones(project_id)
    by_photo: dict[str, list] = {}
    for z in zones:
        by_photo.setdefault(z["photo_id"], []).append(z)
    out = []
    for r in store.list_photos(project_id):
        d = photo_dto(r)
        zs = by_photo.get(r["id"], [])
        d["zoneTypes"] = sorted({z["cls"] for z in zs})
        d["zoneIds"] = [z["id"] for z in zs]
        out.append(d)
    return out


def map_config() -> dict:
    return {
        "bbox": config.MAP_START_BBOX,
        "tiles": config.TILES,
        "classes": [
            {"type": c, "label": config.CLASS_LABELS_RU[c], "level": config.SEVERITY[c],
             "threshold": config.CLS_CONF[c]}
            for c in config.CLASSES
        ],
        "processingBudgetSec": config.PROCESSING_BUDGET_MS // 1000,
    }
