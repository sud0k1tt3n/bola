"""
Обёртка над YOLO11n (ultralytics; подходят и веса YOLOv8) для детекции опасных явлений на снимках БАС.

Режимы (выбираются автоматически, см. config.DETECTOR_MODE):
  custom    — models/best.pt, дообученный на 3 класса
              (flooding, bridge_collapse, fallen_tree). Рабочий режим.
  coco_stub — best.pt нет: грузим COCO yolo11n.pt и выдаём несколько
              классов COCO за наши (config.COCO_STUB_MAP). Нужен только чтобы
              прототип запускался «из коробки»; результаты смысла не имеют.
  demo      — RHD_DEMO=1 или RHD_DETECTOR=demo: на любом снимке два
              фиксированных участка (красный и жёлтый) — проверка интерфейса.
  mock      — ultralytics/torch не установлены (или RHD_DETECTOR=mock):
              цветовая эвристика «много синей воды → flooding». Позволяет
              разрабатывать фронтенд и тестировать API без нейросети.

Во всех режимах наружу отдаются только наши 3 класса, отфильтрованные по
порогам CLS_CONF и размеченные severity по жёсткому маппингу из config.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

import config

log = logging.getLogger(__name__)


@dataclass
class Detection:
    cls: str
    confidence: float
    bbox_px: tuple[float, float, float, float]  # x1, y1, x2, y2 в пикселях кадра

    @property
    def severity(self) -> str:
        return config.SEVERITY[self.cls]


def overall_status(detections: list[Detection]) -> str:
    """Хотя бы один red → red; иначе хотя бы один yellow → yellow; иначе green."""
    severities = {d.severity for d in detections}
    if "red" in severities:
        return "red"
    if "yellow" in severities:
        return "yellow"
    return "green"


def apply_thresholds(dets: list[Detection]) -> list[Detection]:
    """Отбрасываем чужие классы и всё, что ниже порога своего класса."""
    kept = [
        d for d in dets
        if d.cls in config.SEVERITY and d.confidence >= config.CLS_CONF[d.cls]
    ]
    # Сначала самые опасные и уверенные — удобно для списка на фронтенде.
    order = {"red": 0, "yellow": 1}
    kept.sort(key=lambda d: (order[d.severity], -d.confidence))
    return kept


class HazardDetector:
    """Ленивая инициализация один раз при старте приложения."""

    def __init__(self, mode: Optional[str] = None):
        mode = (mode or config.DETECTOR_MODE).lower()
        self.mode = "mock"
        self.weights: Optional[str] = None
        self.warning: Optional[str] = None
        self._model = None
        self._names: dict[int, str] = {}

        if mode not in ("auto", "custom", "coco_stub", "mock", "demo"):
            raise ValueError(f"Неизвестный RHD_DETECTOR={mode}")
        if mode == "demo":
            self.mode = "demo"
            self.warning = (
                "Демо-режим: нейросеть не используется, на каждом снимке рисуются два "
                "тестовых участка — красный (подтопление) и жёлтый (упавшее дерево)."
            )
            log.warning(self.warning)
            return
        if mode == "mock":
            self._use_mock("RHD_DETECTOR=mock")
            return

        try:
            from ultralytics import YOLO  # тяжёлый импорт (torch) — только тут
        except ImportError as exc:
            if mode != "auto":
                raise
            self._use_mock(f"ultralytics не установлен ({exc})")
            return

        if mode in ("auto", "custom") and config.CUSTOM_WEIGHTS.exists():
            self._load(YOLO, config.CUSTOM_WEIGHTS, "custom")
            unknown = set(self._names.values()) - set(config.CLASSES)
            if unknown:
                log.error("В best.pt есть классы вне ТЗ (будут проигнорированы): %s", unknown)
            missing = set(config.CLASSES) - set(self._names.values())
            if missing:
                log.error("В best.pt нет классов %s — они никогда не будут найдены", missing)
            return
        if mode == "custom":
            raise FileNotFoundError(f"Не найдены веса {config.CUSTOM_WEIGHTS}")

        # auto/coco_stub без best.pt → COCO-заглушка
        self._load(YOLO, config.FALLBACK_WEIGHTS, "coco_stub")
        self.warning = (
            f"Файл {config.CUSTOM_WEIGHTS.name} не найден — работает COCO-модель "
            f"{config.FALLBACK_WEIGHTS} с классами-заглушками {config.COCO_STUB_MAP}. "
            "Результаты демонстрационные, не для принятия решений."
        )
        log.warning(self.warning)

    # ------------------------------------------------------------------
    def _load(self, yolo_cls, weights, mode: str) -> None:
        log.info("Загрузка YOLO: %s (режим %s)", weights, mode)
        self._model = yolo_cls(str(weights))
        self._names = dict(self._model.names)
        self.mode = mode
        self.weights = Path(str(weights)).name
        # Прогрев: первый вызов на CPU в разы медленнее последующих.
        self._model.predict(
            np.zeros((config.IMGSZ, config.IMGSZ, 3), dtype=np.uint8),
            imgsz=config.IMGSZ, device=config.DEVICE, verbose=False,
        )

    def _use_mock(self, reason: str) -> None:
        self.mode = "mock"
        self.weights = None
        self.warning = (
            f"Нейросеть не загружена: {reason}. Работает цветовая эвристика "
            "(синяя вода → flooding) только для демонстрации интерфейса."
        )
        log.warning(self.warning)

    @property
    def info(self) -> dict:
        return {"mode": self.mode, "weights": self.weights, "warning": self.warning}

    # ------------------------------------------------------------------
    def detect(self, img: Image.Image) -> list[Detection]:
        """img — RGB, уже развернутый по EXIF Orientation."""
        if self.mode == "demo":
            raw = self._detect_demo(img)
        elif self.mode == "mock":
            raw = self._detect_mock(img)
        else:
            raw = self._detect_yolo(img)
        return apply_thresholds(raw)

    def _detect_yolo(self, img: Image.Image) -> list[Detection]:
        result = self._model.predict(
            img,  # ultralytics сам ресайзит до imgsz с сохранением пропорций
            imgsz=config.IMGSZ,
            conf=config.BASE_CONF,
            iou=config.IOU,
            max_det=config.MAX_DET,
            device=config.DEVICE,
            verbose=False,
        )[0]
        out: list[Detection] = []
        if result.boxes is None or len(result.boxes) == 0:
            return out
        xyxy = result.boxes.xyxy.cpu().numpy()   # координаты уже в пикселях исходника
        confs = result.boxes.conf.cpu().numpy()
        clss = result.boxes.cls.cpu().numpy().astype(int)
        for box, conf, cls_id in zip(xyxy, confs, clss):
            name = self._names.get(int(cls_id), str(cls_id))
            if self.mode == "coco_stub":
                name = config.COCO_STUB_MAP.get(name)
                if name is None:
                    continue
            out.append(Detection(name, round(float(conf), 4), _clip_box(box, img.size)))
        return out

    @staticmethod
    def _detect_demo(img: Image.Image) -> list[Detection]:
        """Фиксированные участки из config.DEMO_DETECTIONS — для проверки всей цепочки."""
        w, h = img.size
        return [
            Detection(cls, conf, _clip_box((x1 * w, y1 * h, x2 * w, y2 * h), (w, h)))
            for cls, conf, (x1, y1, x2, y2) in config.DEMO_DETECTIONS
        ]

    @staticmethod
    def _detect_mock(img: Image.Image) -> list[Detection]:
        """Крупные синие/сине-зелёные области → flooding (НЕ нейросеть)."""
        import cv2

        w, h = img.size
        scale = 640 / max(w, h)
        small = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        hsv = cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2HSV)
        # OpenCV: H ∈ [0..180]. 85–130 ≈ голубой…синий.
        mask = cv2.inRange(hsv, (85, 60, 40), (130, 255, 255))
        kernel = np.ones((5, 5), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        frame_area = mask.shape[0] * mask.shape[1]
        out = []
        for c in contours:
            area = cv2.contourArea(c)
            frac = area / frame_area
            if frac < 0.02:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            conf = round(min(0.95, 0.4 + frac * 2), 4)
            box = (x / scale, y / scale, (x + bw) / scale, (y + bh) / scale)
            out.append(Detection("flooding", conf, _clip_box(box, (w, h))))
        return out


def _clip_box(box, size) -> tuple[float, float, float, float]:
    w, h = size
    x1, y1, x2, y2 = (float(v) for v in box)
    x1, x2 = sorted((min(max(x1, 0), w), min(max(x2, 0), w)))
    y1, y2 = sorted((min(max(y1, 0), h), min(max(y2, 0), h)))
    return (round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1))
