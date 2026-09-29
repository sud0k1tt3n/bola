"""
Геопривязка аэрофотоснимка БАС: EXIF → footprint на земле → bbox → WGS84.

Модель съёмки (MVP)
-------------------
* Камера смотрит строго вниз (надир), рельеф плоский.
* Верх кадра направлен по курсу yaw (градусы по часовой от севера).
* Центр кадра проецируется в точку (lat0, lon0) из GPS.
* Размер кадра на земле считается из высоты над землёй и углов обзора:
      ширина  = 2 * H * tan(HFOV / 2)
      высота  = 2 * H * tan(VFOV / 2)
* Метры → градусы — локальная равнопромежуточная аппроксимация:
      dLat = dy / 111320,   dLon = dx / (111320 * cos(lat0))
  Ошибка на кадре в сотни метров — сантиметры, что много меньше ошибок
  GPS/высоты. Для кадров > 2–3 км стоит перейти на pyproj (UTM).

Системы координат
-----------------
* Пиксель (x, y): x вправо, y вниз, (0,0) — левый верхний угол.
* Нормированные (u, v) = (x / W, y / H) ∈ [0..1].
* Локальные метры относительно центра кадра: E (восток), N (север).
* Все выходные координаты — порядок GeoJSON: [lon, lat].

Модуль не зависит от FastAPI и нейросети — его можно тестировать отдельно.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable, Optional, Sequence

from PIL import Image

import config

log = logging.getLogger(__name__)

# Коды тегов EXIF (TIFF/EXIF 2.3)
TAG_ORIENTATION = 0x0112
TAG_EXIF_IFD = 0x8769
TAG_GPS_IFD = 0x8825
TAG_FOCAL_LENGTH = 0x920A
TAG_FOCAL_35MM = 0xA405

GPS_LAT_REF, GPS_LAT = 1, 2
GPS_LON_REF, GPS_LON = 3, 4
GPS_ALT_REF, GPS_ALT = 5, 6
GPS_IMG_DIR_REF, GPS_IMG_DIR = 16, 17

# Как поворот по EXIF Orientation меняет направление «верха» кадра.
# 6: снимок надо повернуть на 90° по часовой → исходный «верх» (курс) уходит
#    вправо, значит новый верх смотрит на yaw − 90°. 8 — наоборот, 3 — на 180°.
ORIENTATION_YAW_OFFSET = {1: 0.0, 3: 180.0, 6: -90.0, 8: 90.0}


class GeorefError(ValueError):
    """Недостаточно данных для геопривязки (→ HTTP 422)."""

    def __init__(self, message: str, missing: Sequence[str] = ()):
        super().__init__(message)
        self.missing = list(missing)


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------
@dataclass
class ExifData:
    """Сырые значения, извлечённые из EXIF/XMP (всё может быть None)."""

    lat: Optional[float] = None
    lon: Optional[float] = None
    gps_altitude: Optional[float] = None      # обычно над уровнем моря (AMSL)
    relative_altitude: Optional[float] = None  # DJI XMP: над точкой взлёта
    img_direction: Optional[float] = None      # GPSImgDirection
    gimbal_yaw: Optional[float] = None         # DJI XMP
    flight_yaw: Optional[float] = None         # DJI XMP
    gimbal_pitch: Optional[float] = None       # DJI XMP (−90 = надир)
    focal_mm: Optional[float] = None
    focal_35mm: Optional[float] = None
    orientation: int = 1
    errors: list[str] = field(default_factory=list)


@dataclass
class GeoParams:
    """Итоговые параметры съёмки, по которым строится footprint."""

    lat: float
    lon: float
    altitude_m: float  # над землёй
    yaw_deg: float     # куда смотрит верх кадра, по часовой от севера
    hfov_deg: float
    vfov_deg: float
    sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. Чтение EXIF / XMP
# ---------------------------------------------------------------------------
def _to_float(value: Any) -> Optional[float]:
    """IFDRational / tuple(num, den) / int / str → float. Ошибки → None."""
    if value is None:
        return None
    try:
        if isinstance(value, tuple) and len(value) == 2:
            num, den = value
            return float(num) / float(den) if den else None
        if isinstance(value, bytes):
            value = value.decode(errors="ignore").strip("\x00 ")
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _ref(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    return str(value or "").strip("\x00 ").upper()


def dms_to_deg(dms: Any, ref: Any) -> Optional[float]:
    """(град, мин, сек) + 'N'/'S'/'E'/'W' → десятичные градусы."""
    if dms is None:
        return None
    try:
        parts = [_to_float(p) for p in dms]
    except TypeError:  # одиночное число вместо кортежа
        parts = [_to_float(dms)]
    if not parts or any(p is None for p in parts):
        return None
    parts += [0.0] * (3 - len(parts))
    deg = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
    return -deg if _ref(ref) in ("S", "W") else deg


_XMP_ATTR = r'{name}\s*=\s*"([+-]?[0-9.]+)"|<{name}>\s*([+-]?[0-9.]+)\s*</{name}>'


def _xmp_value(xmp: str, name: str) -> Optional[float]:
    """Достаёт числовой атрибут вида drone-dji:Name="+12.3" (или элемент)."""
    m = re.search(_XMP_ATTR.format(name=re.escape(name)), xmp)
    if not m:
        return None
    return _to_float(m.group(1) or m.group(2))


def extract_xmp(raw: bytes) -> str:
    """XMP-пакет лежит в JPEG открытым текстом — ищем его без доп. библиотек."""
    start = raw.find(b"<x:xmpmeta")
    if start < 0:
        return ""
    end = raw.find(b"</x:xmpmeta>", start)
    if end < 0:
        return ""
    return raw[start : end + len(b"</x:xmpmeta>")].decode("utf-8", errors="ignore")


def read_exif(img: Image.Image, raw: bytes = b"") -> ExifData:
    """
    Извлекает всё, что нужно для геопривязки. Никогда не бросает исключений:
    битые теги пишутся в .errors, а соответствующее поле остаётся None.
    """
    data = ExifData()
    try:
        exif = img.getexif()
    except Exception as exc:  # повреждённый APP1 и т.п.
        data.errors.append(f"EXIF не читается: {exc}")
        exif = None

    if exif:
        try:
            data.orientation = int(exif.get(TAG_ORIENTATION, 1) or 1)
        except (TypeError, ValueError):
            data.orientation = 1

        try:
            gps = exif.get_ifd(TAG_GPS_IFD) or {}
        except Exception as exc:
            data.errors.append(f"GPS IFD не читается: {exc}")
            gps = {}
        data.lat = dms_to_deg(gps.get(GPS_LAT), gps.get(GPS_LAT_REF, "N"))
        data.lon = dms_to_deg(gps.get(GPS_LON), gps.get(GPS_LON_REF, "E"))
        alt = _to_float(gps.get(GPS_ALT))
        if alt is not None:
            alt_ref = gps.get(GPS_ALT_REF, 0)
            if isinstance(alt_ref, bytes):
                alt_ref = alt_ref[0] if alt_ref else 0
            data.gps_altitude = -alt if alt_ref == 1 else alt
        data.img_direction = _to_float(gps.get(GPS_IMG_DIR))

        try:
            exif_ifd = exif.get_ifd(TAG_EXIF_IFD) or {}
        except Exception as exc:
            data.errors.append(f"EXIF IFD не читается: {exc}")
            exif_ifd = {}
        data.focal_mm = _to_float(exif_ifd.get(TAG_FOCAL_LENGTH))
        data.focal_35mm = _to_float(exif_ifd.get(TAG_FOCAL_35MM))

    # DJI и многие другие БАС пишут точную ориентацию подвеса в XMP.
    xmp = extract_xmp(raw) if raw else ""
    if xmp:
        data.relative_altitude = _xmp_value(xmp, "drone-dji:RelativeAltitude")
        data.gimbal_yaw = _xmp_value(xmp, "drone-dji:GimbalYawDegree")
        data.flight_yaw = _xmp_value(xmp, "drone-dji:FlightYawDegree")
        data.gimbal_pitch = _xmp_value(xmp, "drone-dji:GimbalPitchDegree")

    # Координаты (0, 0) — типичный мусор от камеры без фикса GPS.
    if data.lat is not None and data.lon is not None:
        if abs(data.lat) < 1e-9 and abs(data.lon) < 1e-9:
            data.errors.append("GPS в EXIF = (0, 0): фикса спутников не было")
            data.lat = data.lon = None
        elif not (-90 <= data.lat <= 90 and -180 <= data.lon <= 180):
            data.errors.append("GPS в EXIF вне допустимого диапазона")
            data.lat = data.lon = None
    return data


# ---------------------------------------------------------------------------
# 2. Углы обзора
# ---------------------------------------------------------------------------
def compute_fov(
    img_w: int,
    img_h: int,
    focal_35mm: Optional[float] = None,
    focal_mm: Optional[float] = None,
    sensor_width_mm: float = config.DEFAULT_SENSOR_WIDTH_MM,
) -> tuple[float, float, str]:
    """
    Возвращает (HFOV, VFOV, источник) в градусах.

    HFOV = 2 * atan(36 мм / (2 * f35)) — ширина полнокадрового сенсора 36 мм.
    VFOV считаем по той же «эквивалентной» ширине, умноженной на соотношение
    сторон реального снимка (36 * H / W): так корректно и для 4:3, и для 16:9,
    а не только для 3:2, где высота сенсора ровно 24 мм.
    """
    aspect = img_h / img_w
    if focal_35mm and focal_35mm > 0:
        sensor_w, focal, src = 36.0, focal_35mm, "exif:FocalLengthIn35mmFilm"
    elif focal_mm and focal_mm > 0:
        sensor_w, focal, src = sensor_width_mm, focal_mm, "exif:FocalLength+sensor_width"
    else:
        hfov = config.DEFAULT_HFOV_DEG
        half_w = math.tan(math.radians(hfov) / 2)
        vfov = math.degrees(2 * math.atan(half_w * aspect))
        return hfov, vfov, "default"

    hfov = math.degrees(2 * math.atan(sensor_w / (2 * focal)))
    vfov = math.degrees(2 * math.atan(sensor_w * aspect / (2 * focal)))
    return hfov, vfov, src


# ---------------------------------------------------------------------------
# 3. Сведение EXIF и ручного ввода в итоговые параметры
# ---------------------------------------------------------------------------
def resolve_params(
    exif: ExifData,
    img_w: int,
    img_h: int,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    altitude: Optional[float] = None,
    yaw: Optional[float] = None,
    demo_position: Optional[tuple[float, float]] = None,
    demo_force: bool = False,
) -> GeoParams:
    """
    Приоритет: ручной ввод из формы > EXIF/XMP > значения по умолчанию.
    Ручные поля перекрывают EXIF — это нужно, например, чтобы исправить
    высоту, когда в EXIF записана высота над уровнем моря.

    demo_position — подставная точка демо-режима: при demo_force перекрывает
    EXIF, иначе используется только когда GPS в снимке нет.

    Без координат центра работать нельзя → GeorefError (422).
    Без yaw/высоты работаем с допущениями и пишем предупреждения.
    """
    sources: dict[str, str] = {}
    warnings: list[str] = list(exif.errors)

    # --- центр кадра -------------------------------------------------------
    if lat is not None and lon is not None:
        c_lat, c_lon = float(lat), float(lon)
        sources["position"] = "manual"
    elif demo_position and (demo_force or exif.lat is None or exif.lon is None):
        c_lat, c_lon = demo_position
        sources["position"] = "demo"
        warnings.append("Демо-режим: координаты подставлены, реальной геопривязки нет")
    elif exif.lat is not None and exif.lon is not None:
        c_lat, c_lon = exif.lat, exif.lon
        sources["position"] = "exif:GPS"
    else:
        raise GeorefError(
            "В снимке нет GPS-координат в EXIF. Укажите вручную широту (lat) и "
            "долготу (lon) центра кадра, а также курс (yaw) и высоту над землёй "
            "(altitude), если они известны.",
            missing=["lat", "lon"],
        )
    if not (-90 <= c_lat <= 90 and -180 <= c_lon <= 180):
        raise GeorefError("lat должна быть в [-90, 90], lon — в [-180, 180]", ["lat", "lon"])
    if abs(c_lat) > 85:
        raise GeorefError("Широта > 85°: локальная аппроксимация не работает у полюса", ["lat"])

    # --- высота над землёй --------------------------------------------------
    if altitude is not None:
        alt, sources["altitude"] = float(altitude), "manual"
    elif exif.relative_altitude is not None:
        alt, sources["altitude"] = exif.relative_altitude, "xmp:RelativeAltitude"
    elif exif.gps_altitude is not None:
        alt, sources["altitude"] = exif.gps_altitude, "exif:GPSAltitude"
        warnings.append(
            "Высота взята из GPSAltitude — обычно это высота над уровнем моря, "
            "а не над землёй. Footprint может быть сильно завышен; укажите "
            "altitude вручную, если знаете высоту полёта."
        )
    else:
        alt, sources["altitude"] = config.DEFAULT_ALTITUDE_M, "default"
        warnings.append(
            f"Высота неизвестна — принята {config.DEFAULT_ALTITUDE_M:.0f} м. "
            "Размер footprint приблизительный."
        )
    if alt <= 0:
        raise GeorefError("Высота над землёй должна быть > 0 м", ["altitude"])
    lo, hi = config.ALTITUDE_SANE_RANGE
    if not lo <= alt <= hi:
        warnings.append(f"Высота {alt:.0f} м вне типичного диапазона БАС ({lo:.0f}–{hi:.0f} м)")

    # --- курс ---------------------------------------------------------------
    if yaw is not None:
        heading, sources["yaw"] = float(yaw), "manual"
    elif exif.gimbal_yaw is not None:
        heading, sources["yaw"] = exif.gimbal_yaw, "xmp:GimbalYawDegree"
    elif exif.img_direction is not None:
        heading, sources["yaw"] = exif.img_direction, "exif:GPSImgDirection"
    elif exif.flight_yaw is not None:
        heading, sources["yaw"] = exif.flight_yaw, "xmp:FlightYawDegree"
    else:
        heading, sources["yaw"] = 0.0, "default"
        warnings.append("Курс (yaw) неизвестен — считаем, что верх кадра смотрит на север")

    # Если снимок пришлось повернуть по EXIF Orientation — повернуть и курс.
    # Ручной yaw оператор задаёт для кадра «как он выглядит», его не трогаем.
    if sources["yaw"] != "manual" and exif.orientation != 1:
        offset = ORIENTATION_YAW_OFFSET.get(exif.orientation)
        if offset is None:
            warnings.append(f"Зеркальная EXIF Orientation={exif.orientation}: курс может быть неверен")
        else:
            heading += offset
    heading %= 360.0

    # --- надир? -------------------------------------------------------------
    if exif.gimbal_pitch is not None and abs(exif.gimbal_pitch + 90.0) > 10.0:
        warnings.append(
            f"Наклон подвеса {exif.gimbal_pitch:.0f}° (не надир): модель плоского "
            "прямоугольного footprint даёт большую ошибку"
        )

    # --- углы обзора ----------------------------------------------------------
    hfov, vfov, fov_src = compute_fov(img_w, img_h, exif.focal_35mm, exif.focal_mm)
    sources["fov"] = fov_src
    if fov_src == "default":
        warnings.append(
            f"Фокусное расстояние не найдено — принят HFOV {config.DEFAULT_HFOV_DEG}°"
        )

    return GeoParams(c_lat, c_lon, alt, heading, hfov, vfov, sources, warnings)


# ---------------------------------------------------------------------------
# 4. Footprint и пересчёт пикселей в WGS84
# ---------------------------------------------------------------------------
class Georeferencer:
    """
    Отображение «пиксель снимка → WGS84» для одного кадра.

    Footprint строится по четырём углам кадра; любая точка внутри кадра
    получается билинейной интерполяцией между углами. Для надирной модели
    footprint — прямоугольник, и билинейная интерполяция совпадает с
    аффинной, но код остаётся корректным, если углы footprint будут заданы
    иначе (например, в будущем — проекцией с учётом наклона камеры).
    """

    def __init__(self, params: GeoParams, img_w: int, img_h: int):
        if img_w <= 0 or img_h <= 0:
            raise ValueError("Размер изображения должен быть > 0")
        self.p = params
        self.w, self.h = int(img_w), int(img_h)

        alt = params.altitude_m
        self.ground_w_m = 2 * alt * math.tan(math.radians(params.hfov_deg) / 2)
        self.ground_h_m = 2 * alt * math.tan(math.radians(params.vfov_deg) / 2)
        # Ground sample distance — сколько метров в одном пикселе.
        self.gsd_m = self.ground_w_m / self.w

        # Углы кадра в порядке TL, TR, BR, BL (как [lon, lat]).
        self.corners = [self._uv_to_lonlat_direct(u, v) for u, v in ((0, 0), (1, 0), (1, 1), (0, 1))]

    # -- базовое преобразование ------------------------------------------------
    def uv_to_local_m(self, u: float, v: float) -> tuple[float, float]:
        """(u, v) ∈ [0..1] → смещение (E, N) в метрах от центра кадра."""
        x = (u - 0.5) * self.ground_w_m  # вправо по кадру
        y = (0.5 - v) * self.ground_h_m  # вверх по кадру (= вперёд по курсу)
        yaw = math.radians(self.p.yaw_deg)
        # Поворот по часовой стрелке на yaw: «вверх кадра» → азимут yaw.
        east = x * math.cos(yaw) + y * math.sin(yaw)
        north = -x * math.sin(yaw) + y * math.cos(yaw)
        return east, north

    def local_m_to_lonlat(self, east: float, north: float) -> list[float]:
        m_lat = config.METERS_PER_DEG_LAT
        m_lon = m_lat * math.cos(math.radians(self.p.lat))
        return [self.p.lon + east / m_lon, self.p.lat + north / m_lat]

    def _uv_to_lonlat_direct(self, u: float, v: float) -> list[float]:
        return self.local_m_to_lonlat(*self.uv_to_local_m(u, v))

    # -- через footprint (как требует ТЗ) ---------------------------------------
    def uv_to_lonlat(self, u: float, v: float) -> list[float]:
        """Билинейная интерполяция внутри четырёхугольника footprint."""
        u = min(max(u, 0.0), 1.0)
        v = min(max(v, 0.0), 1.0)
        tl, tr, br, bl = self.corners
        w_tl, w_tr = (1 - u) * (1 - v), u * (1 - v)
        w_br, w_bl = u * v, (1 - u) * v
        lon = w_tl * tl[0] + w_tr * tr[0] + w_br * br[0] + w_bl * bl[0]
        lat = w_tl * tl[1] + w_tr * tr[1] + w_br * br[1] + w_bl * bl[1]
        return [round(lon, 8), round(lat, 8)]

    def px_to_lonlat(self, x: float, y: float) -> list[float]:
        return self.uv_to_lonlat(x / self.w, y / self.h)

    # -- GeoJSON ---------------------------------------------------------------
    def footprint_geojson(self) -> dict:
        ring = [[round(c[0], 8), round(c[1], 8)] for c in self.corners]
        return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}

    def bbox_to_geojson(self, bbox: Iterable[float]) -> dict:
        """
        bbox [x1, y1, x2, y2] в пикселях → GeoJSON Polygon.
        Углы bbox идут в том же порядке, что и углы footprint (TL, TR, BR, BL),
        и замыкаются первой точкой. На карте это повёрнутый прямоугольник.
        """
        x1, y1, x2, y2 = bbox
        ring = [
            self.px_to_lonlat(x1, y1),
            self.px_to_lonlat(x2, y1),
            self.px_to_lonlat(x2, y2),
            self.px_to_lonlat(x1, y2),
        ]
        return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}

    def bbox_center(self, bbox: Iterable[float]) -> list[float]:
        x1, y1, x2, y2 = bbox
        return self.px_to_lonlat((x1 + x2) / 2, (y1 + y2) / 2)

    def bbox_area_m2(self, bbox: Iterable[float]) -> float:
        x1, y1, x2, y2 = bbox
        return abs(x2 - x1) * abs(y2 - y1) * (self.ground_w_m / self.w) * (self.ground_h_m / self.h)

    def overlay_bounds(self) -> list[list[float]]:
        """Осевой прямоугольник [[south, west], [north, east]] для L.imageOverlay."""
        lons = [c[0] for c in self.corners]
        lats = [c[1] for c in self.corners]
        return [[min(lats), min(lons)], [max(lats), max(lons)]]

    def overlay_corners_latlng(self) -> dict:
        """Три угла в формате Leaflet [lat, lon] для повёрнутого оверлея."""
        tl, tr, br, bl = self.corners
        return {
            "top_left": [tl[1], tl[0]],
            "top_right": [tr[1], tr[0]],
            "bottom_left": [bl[1], bl[0]],
            "bottom_right": [br[1], br[0]],
        }

    def meta(self) -> dict:
        return {
            "center": [round(self.p.lon, 8), round(self.p.lat, 8)],
            "altitude_m": round(self.p.altitude_m, 2),
            "yaw_deg": round(self.p.yaw_deg, 2),
            "hfov_deg": round(self.p.hfov_deg, 2),
            "vfov_deg": round(self.p.vfov_deg, 2),
            "ground_width_m": round(self.ground_w_m, 2),
            "ground_height_m": round(self.ground_h_m, 2),
            "gsd_cm_per_px": round(self.gsd_m * 100, 2),
            "sources": self.p.sources,
            "warnings": self.p.warnings,
            "overlay_bounds": self.overlay_bounds(),
            "overlay_corners": self.overlay_corners_latlng(),
        }


def build_georeferencer(
    img: Image.Image,
    raw: bytes,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    altitude: Optional[float] = None,
    yaw: Optional[float] = None,
    size_after_transpose: Optional[tuple[int, int]] = None,
    demo_position: Optional[tuple[float, float]] = None,
    demo_force: bool = False,
) -> Georeferencer:
    """
    Точка входа для API: EXIF исходного файла + ручные поля → Georeferencer.
    size_after_transpose — размер кадра после применения EXIF Orientation
    (bbox детектора считаются именно в этой системе).
    """
    exif = read_exif(img, raw)
    w, h = size_after_transpose or img.size
    params = resolve_params(exif, w, h, lat=lat, lon=lon, altitude=altitude, yaw=yaw,
                            demo_position=demo_position, demo_force=demo_force)
    for msg in params.warnings:
        log.warning("georef: %s", msg)
    return Georeferencer(params, w, h)


# ---------------------------------------------------------------------------
# Утилита для генерации тестовых снимков с EXIF (используется в tests/ и data/)
# ---------------------------------------------------------------------------
def deg_to_dms_rational(deg: float) -> tuple:
    """Десятичные градусы → ((d,1),(m,1),(s*1000,1000)) для записи в EXIF."""
    deg = abs(deg)
    d = int(deg)
    m_float = (deg - d) * 60
    m = int(m_float)
    s = Fraction((m_float - m) * 60).limit_denominator(10000)
    return (d, m, float(s))
