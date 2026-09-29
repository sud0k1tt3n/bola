"""Демо-режим (RHD_DEMO=1): любое фото → юг Москвы + красный и жёлтый участок."""
import io
import math
from pathlib import Path

import pytest
from PIL import Image

import config
import services
from detector import HazardDetector
from pipeline import analyze_image
from storage import Storage

SAMPLES = Path(__file__).resolve().parents[2] / "data" / "samples"
SOUTH_MOSCOW = (55.6100, 37.6040)


@pytest.fixture(autouse=True)
def demo_on(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "up")
    monkeypatch.setattr(config, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(config, "DEMO_LOCATION", SOUTH_MOSCOW)
    monkeypatch.setattr(config, "DEMO_FORCE_LOCATION", True)


@pytest.fixture(scope="module")
def det():
    return HazardDetector(mode="demo")


def any_photo(seed=0, size=(1200, 900)):
    """Произвольная «фотография» без EXIF — как снимок из мессенджера."""
    img = Image.effect_noise(size, 40 + seed).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def dist_m(lat1, lon1, lat2, lon2):
    k = 111_320
    return math.hypot((lat1 - lat2) * k, (lon1 - lon2) * k * math.cos(math.radians(lat1)))


def test_any_photo_gets_red_and_yellow_in_south_moscow(det):
    res = analyze_image(any_photo(), det)
    assert res["model"]["mode"] == "demo"
    assert res["overall_status"] == "red"
    assert sorted(d["severity"] for d in res["detections"]) == ["red", "yellow"]
    assert {d["class"] for d in res["detections"]} == {"flooding", "fallen_tree"}
    lon, lat = res["georef"]["center"]
    assert res["georef"]["sources"]["position"] == "demo"
    assert dist_m(lat, lon, *SOUTH_MOSCOW) <= config.DEMO_JITTER_M * math.sqrt(2) + 1
    # оба участка внутри кадра на карте и не совпадают
    c1, c2 = (d["center_wgs84"] for d in res["detections"])
    assert c1 != c2


def test_photo_with_foreign_gps_is_moved(det):
    """Снимок с реальным GPS (Нижний Новгород) в демо-режиме всё равно встаёт на юг Москвы."""
    res = analyze_image((SAMPLES / "02_clear_road.jpg").read_bytes(), det)
    lon, lat = res["georef"]["center"]
    assert dist_m(lat, lon, *SOUTH_MOSCOW) < 1000


def test_same_photo_same_place_different_photos_spread(det):
    photo_a, photo_b = any_photo(1), any_photo(2)
    a1 = analyze_image(photo_a, det)["georef"]["center"]
    a2 = analyze_image(photo_a, det)["georef"]["center"]
    b = analyze_image(photo_b, det)["georef"]["center"]
    assert a1 == a2 and a1 != b


def test_manual_coords_still_win(det):
    res = analyze_image(any_photo(), det, lat=59.94, lon=30.31)
    assert res["georef"]["sources"]["position"] == "manual"
    assert res["georef"]["center"] == [30.31, 59.94]


def test_no_force_keeps_exif(det, monkeypatch):
    monkeypatch.setattr(config, "DEMO_FORCE_LOCATION", False)
    res = analyze_image((SAMPLES / "02_clear_road.jpg").read_bytes(), det)
    assert res["georef"]["sources"]["position"] == "exif:GPS"
    res = analyze_image(any_photo(), det)          # без GPS → демо-точка вместо 422
    assert res["georef"]["sources"]["position"] == "demo"


def test_saved_to_project_as_demo(det, tmp_path):
    store = Storage(tmp_path / "d.db")
    res = services.process_upload(store, det, any_photo(3), "IMG_0001.jpg")
    assert res["photo"]["geoSource"] == "demo"
    assert [z["level"] for z in res["zones"]] == ["red", "yellow"]
    assert "Москва" in res["zones"][0]["place"]


def test_location_parsing():
    assert config._parse_location(" 55.61, 37.60 ") == (55.61, 37.60)
    with pytest.raises(ValueError):
        config._parse_location("южное бутово")
