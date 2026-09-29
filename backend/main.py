"""
FastAPI-приложение: приём снимка, детекция, геопривязка, выдача JSON,
плюс проекты / снимки / участки / вердикты для интерфейса оператора.

Запуск:  cd backend && uvicorn main:app --reload --port 8000
Затем:   http://localhost:8000/  (frontend раздаётся этим же сервером)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import Body, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import services
import settlements
from detector import HazardDetector
from pipeline import AnalyzeError, parse_optional_float
from schemas import AnalyzeResponse, ErrorResponse, HealthResponse
from storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("road-hazard")

config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Модель грузится один раз при старте (1–3 с на CPU), а не на каждый запрос.
    app.state.detector = HazardDetector()
    app.state.store = Storage()
    app.state.store.ensure_default_project()
    log.info("Детектор готов: %s", app.state.detector.info)
    yield


app = FastAPI(
    title="АэроРоуд — Road Hazard Detector",
    description="Выявление опасных явлений на дорогах и переправах по снимкам БАС",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=config.CORS_ORIGIN_REGEX,
    allow_origins=config.CORS_EXTRA_ORIGINS,  # "null" — страница открыта как file://
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)


@app.exception_handler(AnalyzeError)
async def analyze_error_handler(_: Request, exc: AnalyzeError):
    body = {"detail": exc.detail, "missing": exc.missing, **exc.extra}
    return JSONResponse(status_code=exc.status, content=body)


def _store(request: Request) -> Storage:
    return request.app.state.store


def _manual(lat, lon, altitude, yaw) -> dict:
    return {
        "lat": parse_optional_float("lat", lat),
        "lon": parse_optional_float("lon", lon),
        "altitude": parse_optional_float("altitude", altitude),
        "yaw": parse_optional_float("yaw", yaw),
    }


# ---------------------------------------------------------------------------
# Основной поток: анализ снимка
# ---------------------------------------------------------------------------
@app.get("/api/health", response_model=HealthResponse)
def health(request: Request):
    return {"status": "ok", "model": request.app.state.detector.info}


@app.post(
    "/api/analyze",
    response_model=AnalyzeResponse,
    responses={400: {"model": ErrorResponse}, 413: {"model": ErrorResponse},
               415: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def analyze(
    request: Request,
    image: UploadFile = File(..., description="JPG/PNG снимок с БАС"),
    # Строки, а не float: пустое поле формы не должно ронять запрос.
    lat: Optional[str] = Form(None, description="Широта центра кадра, если нет EXIF"),
    lon: Optional[str] = Form(None, description="Долгота центра кадра"),
    altitude: Optional[str] = Form(None, description="Высота над землёй, м"),
    yaw: Optional[str] = Form(None, description="Курс верха кадра, ° от севера по часовой"),
    project_id: Optional[str] = Form(None, description="Проект, куда записать снимок"),
    place: Optional[str] = Form(None, description="Подпись участка от оператора"),
):
    # Обычная (не async) функция: FastAPI выполнит её в пуле потоков,
    # и CPU-инференс не заблокирует event loop.
    raw = image.file.read(config.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    return services.process_upload(
        _store(request), request.app.state.detector, raw, image.filename or "snimok.jpg",
        project_id=project_id or None, place=place, **_manual(lat, lon, altitude, yaw),
    )


# ---------------------------------------------------------------------------
# Интерфейс оператора: конфиг, проекты, снимки, участки, настройки, поиск
# ---------------------------------------------------------------------------
@app.get("/api/config")
def get_config():
    return services.map_config()


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@app.get("/api/projects")
def list_projects(request: Request):
    return _store(request).list_projects()


@app.post("/api/projects", status_code=201)
def create_project(request: Request, payload: ProjectIn):
    return _store(request).create_project(payload.name)


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project(request: Request, project_id: str):
    if not _store(request).delete_project(project_id):
        raise AnalyzeError(404, "Проект не найден")
    return Response(status_code=204)


@app.get("/api/photos")
def list_photos(request: Request, projectId: Optional[str] = Query(None)):
    return services.list_photos(_store(request), projectId)


@app.get("/api/photos/{photo_id}")
def get_photo(request: Request, photo_id: str):
    row = _store(request).get_photo(photo_id)
    if row is None:
        raise AnalyzeError(404, "Снимок не найден")
    return services.photo_dto(row, _store(request))


@app.delete("/api/photos/{photo_id}", status_code=204)
def delete_photo(request: Request, photo_id: str):
    _store(request).delete_photo(photo_id)
    return Response(status_code=204)


class GeoIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    altitude: Optional[float] = None
    yaw: Optional[float] = None
    place: Optional[str] = None


@app.post("/api/photos/{photo_id}/geo", response_model=AnalyzeResponse)
def set_photo_geo(request: Request, photo_id: str, payload: GeoIn):
    """Ручная привязка: снимок без GPS (или с неверной высотой/курсом) прогоняется заново."""
    return services.apply_manual_geo(
        _store(request), request.app.state.detector, photo_id, place=payload.place,
        lat=payload.lat, lon=payload.lon, altitude=payload.altitude, yaw=payload.yaw,
    )


@app.get("/api/zones")
def list_zones(request: Request, projectId: Optional[str] = Query(None)):
    return [services.zone_dto(z) for z in _store(request).list_zones(projectId)]


class VerdictIn(BaseModel):
    verdict: Literal["confirmed", "false_positive"]
    comment: Optional[str] = None


@app.post("/api/zones/{zone_id}/verify")
def verify_zone(request: Request, zone_id: str, payload: VerdictIn):
    """Вердикт оператора — материал для дообучения (см. train.py)."""
    store = _store(request)
    if store.get_zone(zone_id) is None:
        raise AnalyzeError(404, "Участок не найден")
    store.set_verdict(zone_id, payload.verdict, payload.comment)
    return services.zone_dto(store.get_zone(zone_id))


@app.get("/api/settings")
def get_settings(request: Request):
    return _store(request).get_settings()


@app.patch("/api/settings")
def patch_settings(request: Request, patch: dict = Body(...)):
    return _store(request).patch_settings(patch)


@app.get("/api/settlements")
def search_settlements(q: str = Query("", max_length=100), limit: int = Query(8, ge=1, le=30)):
    return settlements.search(q, limit)


# Загруженные снимки и миниатюры: /static/uploads/<file>
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")

# Frontend с того же origin — CORS не нужен вовсе. Монтируется последним,
# чтобы не перекрывать /api/*.
if config.FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="frontend")
