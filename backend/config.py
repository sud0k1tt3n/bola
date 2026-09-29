"""
Конфигурация прототипа: пути, пороги confidence, параметры геопривязки.

Всё, что может понадобиться подкрутить без правки логики, собрано здесь.
Любой параметр с префиксом RHD_ можно переопределить переменной окружения.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Пути
# ---------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
MODELS_DIR = BACKEND_DIR / "models"
STATIC_DIR = BACKEND_DIR / "static"
UPLOADS_DIR = STATIC_DIR / "uploads"
FRONTEND_DIR = PROJECT_DIR / "frontend"

# База проектов/снимков/участков (sqlite) и снимки, ждущие ручной привязки.
DATA_DIR = Path(os.getenv("RHD_DATA_DIR", str(BACKEND_DIR / "data")))
DB_PATH = DATA_DIR / "aeroroad.db"
PENDING_DIR = DATA_DIR / "pending"

# Архитектура: стартовые COCO-веса для обучения и для заглушки без best.pt.
# YOLO11 по умолчанию; вернуться на v8 — RHD_BASE_WEIGHTS=yolov8n.pt,
# модель побольше — yolo11s.pt (точнее, ~2.5× медленнее на CPU).
BASE_WEIGHTS = os.getenv("RHD_BASE_WEIGHTS", "yolo11n.pt")

# Дообученные веса (3 класса). Если файла нет — fallback на COCO BASE_WEIGHTS.
CUSTOM_WEIGHTS = MODELS_DIR / "best.pt"
FALLBACK_WEIGHTS = os.getenv("RHD_FALLBACK_WEIGHTS", BASE_WEIGHTS)

# Режим детектора: auto | custom | coco_stub | mock
#   auto      — best.pt → yolo11n.pt (COCO-заглушка) → mock, что первым заработает
#   mock      — без нейросети: цветовая эвристика «вода» (для демо фронтенда и
#               тестов, когда ultralytics/torch не установлены)
DETECTOR_MODE = os.getenv("RHD_DETECTOR", "auto").lower()

# ---------------------------------------------------------------------------
# Демо-режим для проверки работоспособности без модели:  RHD_DEMO=1
#   * детектор = "demo": на ЛЮБОМ снимке два фиксированных участка —
#     красный (flooding) и жёлтый (fallen_tree);
#   * любой снимок ставится на юг Москвы (EXIF-координаты игнорируются,
#     ручные lat/lon из формы по-прежнему главнее). Каждый следующий снимок
#     сдвигается на несколько сотен метров, чтобы кадры не легли стопкой.
# Всё можно включать и по отдельности:
#   RHD_DETECTOR=demo                    — только фиксированные участки
#   RHD_DEMO_LOCATION="55.6100,37.6040"  — своя точка вместо юга Москвы
#   RHD_DEMO_FORCE_LOCATION=0            — точку брать, только если в снимке нет GPS
# ---------------------------------------------------------------------------
DEMO = os.getenv("RHD_DEMO", "0").lower() in ("1", "true", "yes", "on")
if DEMO and "RHD_DETECTOR" not in os.environ:
    DETECTOR_MODE = "demo"


def _parse_location(value: str):
    try:
        lat, lon = (float(v) for v in value.replace(" ", "").split(","))
        return lat, lon
    except ValueError:
        raise ValueError(f"RHD_DEMO_LOCATION должен быть вида '55.61,37.60', получено: {value!r}")


# Юг Москвы, Чертаново (Варшавское шоссе). None — подмена координат выключена.
DEMO_LOCATION = (
    _parse_location(os.environ["RHD_DEMO_LOCATION"]) if os.getenv("RHD_DEMO_LOCATION")
    else (55.6100, 37.6040) if DEMO else None
)
DEMO_FORCE_LOCATION = os.getenv("RHD_DEMO_FORCE_LOCATION", "1" if DEMO else "0").lower() in ("1", "true", "yes", "on")
DEMO_JITTER_M = 600  # разброс последовательных снимков вокруг точки

# Фиксированные участки демо-детектора: доли кадра [x1, y1, x2, y2].
DEMO_DETECTIONS = [
    ("flooding", 0.87, (0.12, 0.48, 0.50, 0.86)),     # красный — внизу слева
    ("fallen_tree", 0.64, (0.56, 0.16, 0.86, 0.40)),  # жёлтый — вверху справа
]

# ---------------------------------------------------------------------------
# Классы, пороги и severity
# ---------------------------------------------------------------------------
CLASSES = ("flooding", "bridge_collapse", "fallen_tree")

# Порог confidence отдельно для каждого класса.
# Подтопление ловим чуть агрессивнее: пропуск опаснее ложной тревоги.
CLS_CONF = {
    "flooding": 0.35,
    "bridge_collapse": 0.40,
    "fallen_tree": 0.40,
}

# Жёсткий маппинг класс → уровень опасности (менять только осознанно).
SEVERITY = {
    "flooding": "red",          # непроходимо
    "bridge_collapse": "red",   # непроходимо
    "fallen_tree": "yellow",    # риск
}

CLASS_LABELS_RU = {
    "flooding": "Подтопление",
    "bridge_collapse": "Обрушение моста",
    "fallen_tree": "Упавшее дерево",
}

# Заглушка для COCO-весов: какие классы COCO выдавать за наши.
# Это ТОЛЬКО для проверки конвейера «из коробки» — смысловой точности нет.
COCO_STUB_MAP = {
    "boat": "flooding",
    "train": "bridge_collapse",
    "potted plant": "fallen_tree",
}

# ---------------------------------------------------------------------------
# Инференс
# ---------------------------------------------------------------------------
IMGSZ = int(os.getenv("RHD_IMGSZ", "640"))
DEVICE = os.getenv("RHD_DEVICE", "cpu")
# Нижняя граница для самого YOLO; финальная фильтрация — по CLS_CONF.
BASE_CONF = min(CLS_CONF.values())
IOU = 0.5
MAX_DET = 100
PROCESSING_BUDGET_MS = 30_000  # целевое ограничение ТЗ: ≤ 30 с на кадр

# ---------------------------------------------------------------------------
# Загрузка файлов
# ---------------------------------------------------------------------------
MAX_UPLOAD_MB = int(os.getenv("RHD_MAX_UPLOAD_MB", "40"))
ALLOWED_FORMATS = {"JPEG", "PNG"}
CROP_PADDING = 0.15  # запас вокруг bbox для миниатюры в popup

# ---------------------------------------------------------------------------
# Геопривязка
# ---------------------------------------------------------------------------
METERS_PER_DEG_LAT = 111_320.0

# Если в EXIF нет фокусного расстояния в 35-мм эквиваленте, пробуем
# FocalLength + ширину сенсора. Значение по умолчанию — 1" сенсор 13.2 мм
# (DJI Phantom 4 Pro / Mavic 2 Pro). Для других бортов укажите свою.
DEFAULT_SENSOR_WIDTH_MM = float(os.getenv("RHD_SENSOR_WIDTH_MM", "13.2"))

# Если нет ни одного фокусного — берём типичный HFOV DJI (84° по диагонали,
# ≈ 73.7° по горизонтали для 3:2 / 24 мм экв.).
DEFAULT_HFOV_DEG = float(os.getenv("RHD_DEFAULT_HFOV_DEG", "73.7"))

# Высота над землёй по умолчанию, если её нет нигде (м).
DEFAULT_ALTITUDE_M = float(os.getenv("RHD_DEFAULT_ALTITUDE_M", "100"))

# Разумные пределы высоты полёта БАС над землёй (м): вне их — предупреждение.
ALTITUDE_SANE_RANGE = (5.0, 500.0)

# ---------------------------------------------------------------------------
# Карта: подложки для фронтенда (GET /api/config). Спутник и подписи — Esri,
# схема — OpenStreetMap. В закрытом контуре замените на свой тайл-сервер.
# ---------------------------------------------------------------------------
MAP_START_BBOX = {"west": 30.0, "south": 50.0, "east": 60.0, "north": 62.0}  # европейская часть РФ
TILES = {
    "satellite": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Спутник: Esri, Maxar, Earthstar Geographics",
        "maxNativeZoom": 19,
    },
    # Гибрид = спутник + прозрачные слои подписей и дорог поверх него.
    "labels": [
        {"url": "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
         "attribution": "Подписи: Esri", "maxNativeZoom": 19},
        {"url": "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}",
         "attribution": "", "maxNativeZoom": 19},
    ],
    "scheme": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© участники OpenStreetMap",
        "maxNativeZoom": 19,
    },
}

# ---------------------------------------------------------------------------
# CORS: любые порты localhost/127.0.0.1 + file:// (Origin: null)
# ---------------------------------------------------------------------------
CORS_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
CORS_EXTRA_ORIGINS = ["null"]
