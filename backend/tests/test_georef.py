"""Тесты геопривязки: чистая математика + чтение EXIF."""
import io
import math

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

import georef
from georef import ExifData, Georeferencer, GeorefError, compute_fov, resolve_params


def haversine_m(a, b):
    """Расстояние между [lon, lat] точками, м — независимая проверка."""
    r = 6_371_008.8
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def bearing_deg(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return math.degrees(math.atan2(y, x)) % 360


def make_geo(yaw=0.0, alt=100.0, lat=55.75, lon=37.62, w=4000, h=3000, f35=24.0):
    exif = ExifData(lat=lat, lon=lon, relative_altitude=alt, img_direction=yaw, focal_35mm=f35)
    return Georeferencer(resolve_params(exif, w, h), w, h)


# --- углы обзора ---------------------------------------------------------------
def test_fov_from_35mm():
    hfov, vfov, src = compute_fov(4000, 3000, focal_35mm=24)
    assert src == "exif:FocalLengthIn35mmFilm"
    assert hfov == pytest.approx(73.74, abs=0.01)          # 2*atan(18/24)
    assert vfov == pytest.approx(58.72, abs=0.01)          # 2*atan(13.5/24), 4:3


def test_fov_from_focal_and_sensor():
    hfov, _, src = compute_fov(5472, 3648, focal_mm=8.8, sensor_width_mm=13.2)
    assert src.startswith("exif:FocalLength")
    assert hfov == pytest.approx(73.7, abs=0.1)            # Phantom 4 Pro ≈ 24 мм экв.


def test_fov_default():
    hfov, _, src = compute_fov(4000, 3000)
    assert src == "default" and hfov > 0


# --- размеры footprint ---------------------------------------------------------
def test_footprint_size_formula():
    g = make_geo(alt=100)
    # ширина = 2*H*tan(HFOV/2) = 2*100*18/24 = 150 м; высота = 112.5 м (4:3)
    assert g.ground_w_m == pytest.approx(150.0, rel=1e-9)
    assert g.ground_h_m == pytest.approx(112.5, rel=1e-9)
    assert g.gsd_m == pytest.approx(150 / 4000)


@pytest.mark.parametrize("yaw", [0, 35, 90, 180, 271.5])
def test_footprint_edges_match_ground_size(yaw):
    g = make_geo(yaw=yaw)
    tl, tr, br, bl = g.corners
    assert haversine_m(tl, tr) == pytest.approx(150.0, rel=2e-3)
    assert haversine_m(tr, br) == pytest.approx(112.5, rel=2e-3)
    assert haversine_m(tl, br) == pytest.approx(187.5, rel=2e-3)  # диагональ 150-112.5-187.5


@pytest.mark.parametrize("yaw", [0, 35, 90, 180, 271.5])
def test_top_of_frame_points_to_yaw(yaw):
    g = make_geo(yaw=yaw)
    center = g.uv_to_lonlat(0.5, 0.5)
    top_mid = g.uv_to_lonlat(0.5, 0.0)
    right_mid = g.uv_to_lonlat(1.0, 0.5)
    assert bearing_deg(center, top_mid) == pytest.approx(yaw % 360, abs=0.05)
    # правый край кадра — на 90° по часовой от курса
    assert bearing_deg(center, right_mid) == pytest.approx((yaw + 90) % 360, abs=0.05)


def test_center_pixel_is_gps_point():
    g = make_geo(yaw=123)
    lon, lat = g.px_to_lonlat(2000, 1500)
    assert lon == pytest.approx(37.62, abs=1e-7) and lat == pytest.approx(55.75, abs=1e-7)


def test_yaw_zero_orientation():
    g = make_geo(yaw=0)
    tl, tr, br, bl = g.corners
    assert tl[1] > bl[1] and tl[0] < tr[0]  # верх — север, право — восток
    assert g.overlay_bounds() == [[bl[1], bl[0]], [tr[1], tr[0]]]


def test_full_frame_bbox_equals_footprint():
    g = make_geo(yaw=35)
    assert g.bbox_to_geojson([0, 0, 4000, 3000])["coordinates"] == g.footprint_geojson()["coordinates"]


def test_bbox_polygon_closed_and_area():
    g = make_geo(yaw=35)
    poly = g.bbox_to_geojson([1000, 750, 3000, 2250])
    ring = poly["coordinates"][0]
    assert len(ring) == 5 and ring[0] == ring[-1]
    assert g.bbox_area_m2([1000, 750, 3000, 2250]) == pytest.approx(75 * 56.25)
    assert haversine_m(ring[0], ring[1]) == pytest.approx(75.0, rel=2e-3)


def test_bilinear_is_linear_along_edge():
    g = make_geo(yaw=60)
    a, b = g.uv_to_lonlat(0, 0), g.uv_to_lonlat(1, 0)
    mid = g.uv_to_lonlat(0.25, 0)
    assert haversine_m(a, mid) == pytest.approx(haversine_m(a, b) / 4, rel=1e-3)


def test_southern_western_hemisphere():
    g = make_geo(lat=-33.86, lon=-70.65, yaw=0)
    tl, tr, br, bl = g.corners
    assert haversine_m(tl, tr) == pytest.approx(150.0, rel=2e-3)
    assert tl[1] > bl[1]


# --- сведение параметров ----------------------------------------------------------
def test_missing_gps_raises_422_error():
    with pytest.raises(GeorefError) as e:
        resolve_params(ExifData(), 100, 100)
    assert set(e.value.missing) == {"lat", "lon"}


def test_manual_overrides_exif():
    exif = ExifData(lat=1, lon=2, relative_altitude=50, gimbal_yaw=10, focal_35mm=24)
    p = resolve_params(exif, 400, 300, lat=55, lon=37, altitude=120, yaw=270)
    assert (p.lat, p.lon, p.altitude_m, p.yaw_deg) == (55, 37, 120, 270)
    assert p.sources["position"] == p.sources["altitude"] == p.sources["yaw"] == "manual"


def test_priorities_and_warnings():
    exif = ExifData(lat=55, lon=37, gps_altitude=210, img_direction=20, flight_yaw=99)
    p = resolve_params(exif, 400, 300)
    assert p.sources["altitude"] == "exif:GPSAltitude"
    assert any("над уровнем моря" in w for w in p.warnings)
    assert p.yaw_deg == 20 and p.sources["yaw"] == "exif:GPSImgDirection"
    assert p.sources["fov"] == "default"


def test_defaults_when_only_position():
    p = resolve_params(ExifData(lat=55, lon=37), 400, 300)
    assert p.yaw_deg == 0 and p.sources["yaw"] == "default"
    assert p.sources["altitude"] == "default"
    assert len(p.warnings) >= 3


def test_negative_yaw_normalized():
    p = resolve_params(ExifData(lat=55, lon=37, gimbal_yaw=-45.0), 400, 300)
    assert p.yaw_deg == pytest.approx(315)


@pytest.mark.parametrize("orientation,expected", [(1, 30), (3, 210), (6, 300), (8, 120)])
def test_exif_orientation_rotates_yaw(orientation, expected):
    p = resolve_params(ExifData(lat=55, lon=37, gimbal_yaw=30, orientation=orientation), 400, 300)
    assert p.yaw_deg == pytest.approx(expected)


def test_non_nadir_warning():
    p = resolve_params(ExifData(lat=55, lon=37, gimbal_pitch=-45), 400, 300)
    assert any("надир" in w for w in p.warnings)


@pytest.mark.parametrize("kw", [{"altitude": 0}, {"altitude": -5}, {"lat": 95, "lon": 0}, {"lat": 89, "lon": 0}])
def test_invalid_manual_values(kw):
    base = {"lat": 55, "lon": 37}
    base.update(kw)
    with pytest.raises(GeorefError):
        resolve_params(ExifData(), 400, 300, **base)


# --- чтение EXIF из файла --------------------------------------------------------
def _jpeg(gps=None, exif_ifd=None, orientation=1, xmp=None):
    ex = Image.Exif()
    ex[0x0112] = orientation
    if gps:
        ex[0x8825] = gps
    if exif_ifd:
        ex[0x8769] = exif_ifd
    buf = io.BytesIO()
    kw = {"exif": ex.tobytes()}
    if xmp:
        kw["xmp"] = xmp
    Image.new("RGB", (64, 48), "gray").save(buf, "JPEG", **kw)
    raw = buf.getvalue()
    return Image.open(io.BytesIO(raw)), raw


def test_read_exif_south_west_and_altitude_ref():
    img, raw = _jpeg(gps={
        1: "S", 2: georef.deg_to_dms_rational(33.8688), 3: "W", 4: georef.deg_to_dms_rational(70.6483),
        5: b"\x01", 6: IFDRational(1250, 100), 17: IFDRational(9050, 100),
    }, exif_ifd={0xA405: 28, 0x920A: IFDRational(45, 10)}, orientation=6)
    d = georef.read_exif(img, raw)
    assert d.lat == pytest.approx(-33.8688, abs=1e-6)
    assert d.lon == pytest.approx(-70.6483, abs=1e-6)
    assert d.gps_altitude == pytest.approx(-12.5)
    assert d.img_direction == pytest.approx(90.5)
    assert (d.focal_35mm, d.focal_mm, d.orientation) == (28, 4.5, 6)


def test_read_exif_zero_gps_is_rejected():
    img, raw = _jpeg(gps={1: "N", 2: (0, 0, 0), 3: "E", 4: (0, 0, 0)})
    d = georef.read_exif(img, raw)
    assert d.lat is None and d.errors


def test_read_dji_xmp():
    xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description '
           b'drone-dji:RelativeAltitude="+80.25" drone-dji:GimbalYawDegree="-12.4" '
           b'drone-dji:GimbalPitchDegree="-89.9"/></rdf:RDF></x:xmpmeta>')
    img, raw = _jpeg(xmp=xmp)
    d = georef.read_exif(img, raw)
    assert d.relative_altitude == pytest.approx(80.25)
    assert d.gimbal_yaw == pytest.approx(-12.4)
    assert d.gimbal_pitch == pytest.approx(-89.9)


def test_read_exif_no_exif_png():
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, "PNG")
    d = georef.read_exif(Image.open(io.BytesIO(buf.getvalue())), buf.getvalue())
    assert d.lat is None and d.lon is None and d.orientation == 1


def test_dms_helpers():
    assert georef.dms_to_deg((55, 45, 0), "N") == pytest.approx(55.75)
    assert georef.dms_to_deg((55, 45, 0), b"S") == pytest.approx(-55.75)
    assert georef.dms_to_deg(None, "N") is None
    assert georef.dms_to_deg(((1, 0), 0, 0), "N") is None  # деление на ноль → None
