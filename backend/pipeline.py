"""
Конвейер анализа одного снимка — без привязки к HTTP.

main.py лишь принимает multipart и отдаёт результат этой функции; так весь
путь «байты файла → JSON» можно тестировать без поднятия сервера.
"""
from __future__ import annotations

import hashlib
import io
import math
import logging
import threading
import time
import uuid
from typing import Optional

from PIL import Image, ImageOps, UnidentifiedImageError

import config
from detector import Detection, HazardDetector, overall_status
from georef import Georeferencer, GeorefError, build_georeferencer

log = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = 200_000_000  # снимки БАС бывают по 48–100 Мп

# ultralytics-модель не гарантирует потокобезопасность → один инференс за раз.
_infer_lock = threading.Lock()


class AnalyzeError(Exception):
    """Ошибка, которую нужно вернуть клиенту с конкретным HTTP-кодом."""

    def __init__(self, status: int, detail: str, missing: Optional[list[str]] = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.missing = missing or []
        self.extra: dict = {}  # доп. поля тела ответа (например, photo_id)


def parse_optional_float(name: str, value: Optional[str]) -> Optional[float]:
    """Поля формы приходят строками; пустая строка = не задано; '55,75' тоже ок."""
    if value is None:
        return None
    value = str(value).strip().replace(",", ".")
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        raise AnalyzeError(422, f"Поле {name}: '{value}' — не число", [name])


def _demo_position(raw: bytes) -> Optional[tuple[float, float]]:
    """
    Точка демо-режима со сдвигом до DEMO_JITTER_M, зависящим от содержимого
    файла: один и тот же снимок всегда встаёт в одно место, разные — рядом.
    """
    if not config.DEMO_LOCATION:
        return None
    lat0, lon0 = config.DEMO_LOCATION
    digest = hashlib.sha1(raw).digest()
    dx = (digest[0] / 255 - 0.5) * 2 * config.DEMO_JITTER_M
    dy = (digest[1] / 255 - 0.5) * 2 * config.DEMO_JITTER_M
    m_lat = config.METERS_PER_DEG_LAT
    return lat0 + dy / m_lat, lon0 + dx / (m_lat * math.cos(math.radians(lat0)))


def _save_crop(img: Image.Image, det: Detection, path) -> None:
    """Миниатюра bbox с небольшим запасом по краям — для popup на карте."""
    x1, y1, x2, y2 = det.bbox_px
    pad_x = (x2 - x1) * config.CROP_PADDING
    pad_y = (y2 - y1) * config.CROP_PADDING
    box = (
        int(max(0, x1 - pad_x)), int(max(0, y1 - pad_y)),
        int(min(img.width, x2 + pad_x)), int(min(img.height, y2 + pad_y)),
    )
    if box[2] - box[0] < 2 or box[3] - box[1] < 2:
        return
    crop = img.crop(box)
    crop.thumbnail((320, 320))
    crop.save(path, "JPEG", quality=85)


def analyze_image(
    raw: bytes,
    detector: HazardDetector,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    altitude: Optional[float] = None,
    yaw: Optional[float] = None,
    image_id: Optional[str] = None,
) -> dict:
    """image_id — повторная обработка уже известного снимка (ручная привязка)."""
    t0 = time.perf_counter()

    if not raw:
        raise AnalyzeError(400, "Пустой файл")
    if len(raw) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise AnalyzeError(413, f"Файл больше {config.MAX_UPLOAD_MB} МБ")

    # --- 1. Декодирование ---------------------------------------------------
    try:
        src = Image.open(io.BytesIO(raw))
        fmt = src.format
        src.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise AnalyzeError(400, f"Файл не распознан как изображение: {exc}")
    if fmt not in config.ALLOWED_FORMATS:
        raise AnalyzeError(415, f"Формат {fmt} не поддерживается — нужен JPG или PNG")

    # Разворачиваем по EXIF Orientation: детектор, overlay на карте и bbox
    # должны жить в одной и той же системе координат пикселей.
    img = ImageOps.exif_transpose(src).convert("RGB")

    # --- 2. Геопривязка (до инференса: без координат нет смысла гонять сеть) -
    try:
        geo: Georeferencer = build_georeferencer(
            src, raw, lat=lat, lon=lon, altitude=altitude, yaw=yaw,
            size_after_transpose=img.size,
            demo_position=_demo_position(raw), demo_force=config.DEMO_FORCE_LOCATION,
        )
    except GeorefError as exc:
        raise AnalyzeError(422, str(exc), exc.missing)

    # --- 3. Детекция ------------------------------------------------------------
    with _infer_lock:
        t_inf = time.perf_counter()
        detections = detector.detect(img)
        infer_ms = (time.perf_counter() - t_inf) * 1000
    log.info("Инференс %.0f мс, детекций: %d", infer_ms, len(detections))

    # --- 4. Сохранение снимка и миниатюр --------------------------------------
    image_id = image_id or str(uuid.uuid4())
    config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    img.save(config.UPLOADS_DIR / f"{image_id}.jpg", "JPEG", quality=90)
    thumb = img.copy()
    thumb.thumbnail((160, 160))
    thumb.save(config.UPLOADS_DIR / f"{image_id}_thumb.jpg", "JPEG", quality=80)

    det_out = []
    for i, det in enumerate(detections):
        crop_name = f"{image_id}_det{i}.jpg"
        crop_url = None
        try:
            _save_crop(img, det, config.UPLOADS_DIR / crop_name)
            if (config.UPLOADS_DIR / crop_name).exists():
                crop_url = f"/static/uploads/{crop_name}"
        except OSError as exc:
            log.warning("Не удалось сохранить миниатюру: %s", exc)
        det_out.append({
            "class": det.cls,
            "label_ru": config.CLASS_LABELS_RU[det.cls],
            "confidence": det.confidence,
            "severity": det.severity,
            "bbox_px": list(det.bbox_px),
            # Тот же путь пиксель → норм. → footprint → WGS84, что и для кадра.
            "polygon_wgs84": geo.bbox_to_geojson(det.bbox_px),
            "center_wgs84": geo.bbox_center(det.bbox_px),
            "area_m2": round(geo.bbox_area_m2(det.bbox_px), 1),
            "crop_url": crop_url,
        })

    processing_ms = int(round((time.perf_counter() - t0) * 1000))
    if processing_ms > config.PROCESSING_BUDGET_MS:
        log.warning("Обработка %d мс — превышен бюджет %d мс", processing_ms, config.PROCESSING_BUDGET_MS)

    return {
        "image_id": image_id,
        "image_url": f"/static/uploads/{image_id}.jpg",
        "thumb_url": f"/static/uploads/{image_id}_thumb.jpg",
        "image_width": img.width,
        "image_height": img.height,
        "footprint": geo.footprint_geojson(),
        "detections": det_out,
        "overall_status": overall_status(detections),
        "processing_ms": processing_ms,
        "georef": geo.meta(),
        "model": detector.info,
    }
