"""Сквозной прогон «байты файла → JSON» на тестовых снимках (мок-детектор)."""
import io
from pathlib import Path

import pytest
from PIL import Image

import config
from detector import Detection, HazardDetector, apply_thresholds, overall_status
from pipeline import AnalyzeError, analyze_image, parse_optional_float
from schemas import AnalyzeResponse

SAMPLES = Path(__file__).resolve().parents[2] / "data" / "samples"


@pytest.fixture(scope="module")
def detector():
    return HazardDetector(mode="mock")


@pytest.fixture(autouse=True)
def tmp_uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path)


def sample(name):
    p = SAMPLES / name
    if not p.exists():
        pytest.skip("нет data/samples — запустите python data/make_samples.py")
    return p.read_bytes()


def point_in_polygon(pt, ring):
    x, y = pt
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


# --- правила статуса ---------------------------------------------------------------
def D(cls, conf=0.9):
    return Detection(cls, conf, (0, 0, 10, 10))


def test_overall_status_rules():
    assert overall_status([]) == "green"
    assert overall_status([D("fallen_tree")]) == "yellow"
    assert overall_status([D("fallen_tree"), D("flooding")]) == "red"
    assert overall_status([D("bridge_collapse")]) == "red"


def test_thresholds_per_class():
    dets = [D("flooding", 0.36), D("fallen_tree", 0.39), D("bridge_collapse", 0.40), D("car", 0.99)]
    kept = apply_thresholds(dets)
    assert [d.cls for d in kept] == ["bridge_collapse", "flooding"]  # red сначала, по убыванию conf
    assert config.SEVERITY == {"flooding": "red", "bridge_collapse": "red", "fallen_tree": "yellow"}


# --- сквозные сценарии ---------------------------------------------------------------
def test_flooding_sample_red(detector, tmp_path):
    res = analyze_image(sample("01_flooding_dji.jpg"), detector)
    AnalyzeResponse.model_validate(res)  # соответствует контракту API
    assert res["overall_status"] == "red"
    assert res["model"]["mode"] == "mock"
    geo = res["georef"]
    assert geo["sources"]["altitude"] == "xmp:RelativeAltitude"
    assert geo["altitude_m"] == 120.0 and geo["yaw_deg"] == 35.0
    assert geo["ground_width_m"] == pytest.approx(180.0)  # 2*120*18/24

    det = res["detections"][0]
    assert det["class"] == "flooding" and det["severity"] == "red"
    # Вода нарисована эллипсом (500,380)-(1150,860); bbox должен его накрыть
    x1, y1, x2, y2 = det["bbox_px"]
    assert x1 == pytest.approx(500, abs=40) and x2 == pytest.approx(1150, abs=40)
    assert y1 == pytest.approx(380, abs=40) and y2 == pytest.approx(860, abs=40)
    ring = res["footprint"]["coordinates"][0]
    assert point_in_polygon(det["center_wgs84"], ring)
    assert all(point_in_polygon(p, ring) for p in det["polygon_wgs84"]["coordinates"][0][:-1])
    assert (tmp_path / f"{res['image_id']}.jpg").exists()
    assert det["crop_url"] and (tmp_path / Path(det["crop_url"]).name).exists()
    assert 0 < res["processing_ms"] < config.PROCESSING_BUDGET_MS


def test_clear_road_green(detector):
    res = analyze_image(sample("02_clear_road.jpg"), detector)
    assert res["overall_status"] == "green" and res["detections"] == []
    assert res["georef"]["yaw_deg"] == 300.0
    assert res["georef"]["sources"]["yaw"] == "exif:GPSImgDirection"


def test_no_gps_422(detector):
    with pytest.raises(AnalyzeError) as e:
        analyze_image(sample("04_no_gps.jpg"), detector)
    assert e.value.status == 422 and "lat" in e.value.missing


def test_png_manual_coords(detector):
    raw = sample("05_no_exif.png")
    with pytest.raises(AnalyzeError):
        analyze_image(raw, detector)
    res = analyze_image(raw, detector, lat=55.0, lon=73.4, altitude=100, yaw=90)
    assert res["overall_status"] == "red"
    assert res["georef"]["sources"]["position"] == "manual"


def test_exif_orientation_transposed(detector):
    """Снимок, записанный «лёжа» (Orientation=6), разворачивается до детекции."""
    img = Image.new("RGB", (300, 200), (40, 110, 50))
    ex = Image.Exif()
    ex[0x0112] = 6
    ex[0x8825] = {1: "N", 2: (55.0, 0.0, 0.0), 3: "E", 4: (37.0, 0.0, 0.0)}
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=ex.tobytes())
    res = analyze_image(buf.getvalue(), detector, yaw=None)
    assert (res["image_width"], res["image_height"]) == (200, 300)
    assert res["georef"]["yaw_deg"] == 270.0  # 0 (default) − 90


@pytest.mark.parametrize("raw,status", [(b"", 400), (b"not an image", 400)])
def test_bad_files(detector, raw, status):
    with pytest.raises(AnalyzeError) as e:
        analyze_image(raw, detector)
    assert e.value.status == status


def test_unsupported_format(detector):
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, "GIF")
    with pytest.raises(AnalyzeError) as e:
        analyze_image(buf.getvalue(), detector, lat=1, lon=1)
    assert e.value.status == 415


def test_parse_optional_float():
    assert parse_optional_float("lat", "") is None
    assert parse_optional_float("lat", " 55,75 ") == 55.75
    with pytest.raises(AnalyzeError):
        parse_optional_float("lat", "abc")
