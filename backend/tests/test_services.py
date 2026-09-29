"""Проекты, снимки, участки, ручная привязка — без HTTP (хранилище во временной папке)."""
from pathlib import Path

import pytest

import config
import services
import settlements
from detector import HazardDetector
from pipeline import AnalyzeError
from storage import Storage

SAMPLES = Path(__file__).resolve().parents[2] / "data" / "samples"


@pytest.fixture(scope="module")
def detector():
    return HazardDetector(mode="mock")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "PENDING_DIR", tmp_path / "pending")
    s = Storage(tmp_path / "t.db")
    s.ensure_default_project()
    return s


def raw(name):
    p = SAMPLES / name
    if not p.exists():
        pytest.skip("нет data/samples")
    return p.read_bytes()


def test_default_project_and_crud(store):
    projects = store.list_projects()
    assert len(projects) == 1 and projects[0]["name"] == "Мой проект"
    p = store.create_project("  Ока, паводок 2026 ")
    assert p["name"] == "Ока, паводок 2026" and p["photoCount"] == 0
    assert store.delete_project(p["id"]) and not store.delete_project(p["id"])


def test_upload_saves_photo_and_zones(store, detector):
    pid = store.list_projects()[0]["id"]
    res = services.process_upload(store, detector, raw("01_flooding_dji.jpg"), "01.jpg", project_id=pid)
    assert res["overall_status"] == "red"                    # детекция не изменилась
    photo = res["photo"]
    assert photo["status"] == "done" and photo["geoSource"] == "exif"
    assert photo["zoneTypes"] == ["flooding"] and photo["corners"]["top_left"]
    assert "Каширы" in photo["place"] or "Кашира" in photo["place"]
    zones = services.list_photos(store, pid)
    assert len(zones) == 1
    z = res["zones"][0]
    assert z["type"] == "flooding" and z["level"] == "red" and z["label"] == "Подтопление"
    assert len(z["polygon"]) == 5 and z["centroid"]["lat"] == pytest.approx(54.838, abs=0.01)
    assert store.list_projects()[0]["photoCount"] == 1


def test_need_geo_then_manual(store, detector):
    pid = store.list_projects()[0]["id"]
    with pytest.raises(AnalyzeError) as e:
        services.process_upload(store, detector, raw("05_no_exif.png"), "05.png", project_id=pid)
    photo_id = e.value.extra["photo_id"]
    row = store.get_photo(photo_id)
    assert row["status"] == "need_geo" and Path(row["pending_path"]).exists()

    res = services.apply_manual_geo(store, detector, photo_id, lat=55.75, lon=37.62, yaw=90,
                                    altitude=100, place="М-4, км 112")
    assert res["image_id"] == photo_id
    row = store.get_photo(photo_id)
    assert row["status"] == "done" and row["geo_source"] == "manual" and row["pending_path"] is None
    assert res["zones"][0]["place"] == "М-4, км 112"
    assert res["georef"]["yaw_deg"] == 90.0


def test_manual_regeo_of_done_photo(store, detector):
    res = services.process_upload(store, detector, raw("02_clear_road.jpg"), "02.jpg")
    res2 = services.apply_manual_geo(store, detector, res["image_id"], lat=56.0, lon=44.0, altitude=50)
    assert res2["georef"]["altitude_m"] == 50.0 and res2["overall_status"] == "green"
    assert len(store.list_photos()) == 1


def test_verdict_and_settings(store, detector):
    res = services.process_upload(store, detector, raw("01_flooding_dji.jpg"), "01.jpg")
    zid = res["zones"][0]["id"]
    store.set_verdict(zid, "false_positive", "это пруд")
    z = services.zone_dto(store.get_zone(zid))
    assert z["verdict"] == "false_positive" and z["comment"] == "это пруд"

    s = store.patch_settings({"minConfidence": 60, "alerts": {"nogeo": False}})
    assert s["minConfidence"] == 0.6 and s["alerts"] == {"blocked": True, "nogeo": False}
    assert store.get_settings() == s


def test_settlements():
    assert settlements.search("каз")[0]["name"] == "Казань"
    assert settlements.search("Нижн")[0]["name"] in ("Нижний Новгород", "Нижний Бестях")
    assert settlements.search("масква")[0]["name"] == "Москва"      # опечатка
    assert settlements.search("я") == []
    assert settlements.describe(55.7558, 37.6173).startswith("Москва")
    d = settlements.describe(54.70, 38.30)
    assert "км к" in d and "Каширы" not in d                 # «от г. Кашира» — именительный
    assert settlements.describe(0, 0) is None


def test_map_config():
    cfg = services.map_config()
    assert set(cfg["tiles"]) == {"satellite", "labels", "scheme"}
    assert [c["level"] for c in cfg["classes"]] == ["red", "red", "yellow"]
