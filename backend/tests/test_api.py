"""HTTP-тесты (нужен установленный fastapi; иначе пропускаются)."""
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[2] / "data" / "samples"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("api")
    config.DETECTOR_MODE = "mock"  # тесты API не зависят от весов и torch
    config.DB_PATH = tmp / "api.db"
    config.PENDING_DIR = tmp / "pending"
    import main
    with TestClient(main.app) as c:
        yield c


def post_image(client, name, data=None):
    with open(SAMPLES / name, "rb") as f:
        return client.post("/api/analyze", files={"image": (name, f, "image/jpeg")}, data=data or {})


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_analyze_ok_and_static(client):
    r = post_image(client, "01_flooding_dji.jpg")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overall_status"] == "red"
    assert body["detections"][0]["class"] == "flooding"   # алиас "class", не "cls"
    assert body["photo"]["status"] == "done"
    assert client.get(body["image_url"]).status_code == 200
    assert client.get(body["thumb_url"]).status_code == 200


def test_analyze_422_then_manual_geo(client):
    r = post_image(client, "04_no_gps.jpg")
    assert r.status_code == 422
    body = r.json()
    assert "lat" in body["missing"] and body["photo_id"]
    r = client.post(f"/api/photos/{body['photo_id']}/geo", json={"lat": 55.7, "lon": 37.6, "yaw": 45})
    assert r.status_code == 200, r.text
    assert r.json()["photo"]["geoSource"] == "manual"


def test_analyze_manual_form_fields(client):
    with open(SAMPLES / "05_no_exif.png", "rb") as f:
        r = client.post("/api/analyze", files={"image": ("a.png", f, "image/png")},
                        data={"lat": "55,7", "lon": "37.6", "yaw": "", "altitude": "90"})
    assert r.status_code == 200, r.text


def test_projects_photos_zones_verdict(client):
    pr = client.post("/api/projects", json={"name": "Паводок"}).json()
    r = post_image(client, "01_flooding_dji.jpg", {"project_id": pr["id"]})
    assert r.status_code == 200
    photos = client.get("/api/photos", params={"projectId": pr["id"]}).json()
    zones = client.get("/api/zones", params={"projectId": pr["id"]}).json()
    assert len(photos) == 1 and len(zones) == 1
    v = client.post(f"/api/zones/{zones[0]['id']}/verify", json={"verdict": "confirmed"})
    assert v.status_code == 200 and v.json()["verdict"] == "confirmed"
    assert client.post(f"/api/zones/{zones[0]['id']}/verify", json={"verdict": "maybe"}).status_code == 422
    assert client.delete(f"/api/projects/{pr['id']}").status_code == 204
    assert client.get("/api/zones", params={"projectId": pr["id"]}).json() == []


def test_settings_config_search(client):
    assert client.patch("/api/settings", json={"minConfidence": 0.5}).json()["minConfidence"] == 0.5
    assert client.get("/api/config").json()["tiles"]["satellite"]["url"].startswith("https://")
    assert client.get("/api/settlements", params={"q": "каз"}).json()[0]["name"] == "Казань"


def test_cors_localhost(client):
    r = client.options("/api/analyze", headers={
        "Origin": "http://localhost:5500", "Access-Control-Request-Method": "POST"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5500"
