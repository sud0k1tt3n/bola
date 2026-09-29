"""Pydantic-модели ответа API (контракт между backend и frontend)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

HazardClass = Literal["flooding", "bridge_collapse", "fallen_tree"]
Severity = Literal["red", "yellow"]
Status = Literal["green", "yellow", "red"]


class GeoPolygon(BaseModel):
    """GeoJSON Polygon, координаты [lon, lat] в WGS84, кольцо замкнуто."""

    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[list[float]]]


class DetectionOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # "class" — зарезервированное слово Python, поэтому поле cls с алиасом.
    cls: HazardClass = Field(alias="class")
    label_ru: str
    confidence: float
    severity: Severity
    bbox_px: list[float] = Field(description="[x1, y1, x2, y2] в пикселях снимка")
    polygon_wgs84: GeoPolygon
    center_wgs84: list[float] = Field(description="[lon, lat]")
    area_m2: float
    crop_url: Optional[str] = Field(None, description="Миниатюра области для popup")


class GeorefInfo(BaseModel):
    center: list[float]
    altitude_m: float
    yaw_deg: float
    hfov_deg: float
    vfov_deg: float
    ground_width_m: float
    ground_height_m: float
    gsd_cm_per_px: float
    sources: dict[str, str]
    warnings: list[str]
    overlay_bounds: list[list[float]] = Field(description="[[south, west], [north, east]]")
    overlay_corners: dict[str, list[float]] = Field(description="Углы кадра [lat, lon]")


class ModelInfo(BaseModel):
    mode: Literal["custom", "coco_stub", "mock", "demo"]
    weights: Optional[str]
    warning: Optional[str]


class AnalyzeResponse(BaseModel):
    image_id: str
    image_url: str
    image_width: int
    image_height: int
    footprint: GeoPolygon
    detections: list[DetectionOut]
    overall_status: Status
    processing_ms: int
    georef: GeorefInfo
    model: ModelInfo
    thumb_url: Optional[str] = None
    # Записи хранилища для интерфейса оператора (camelCase, см. services.py)
    photo: Optional[dict] = None
    zones: Optional[list[dict]] = None

    model_config = ConfigDict(protected_namespaces=())


class ErrorResponse(BaseModel):
    detail: str
    missing: list[str] = []
    photo_id: Optional[str] = Field(None, description="Снимок отложен до ручной привязки")


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    model: ModelInfo

    model_config = ConfigDict(protected_namespaces=())
