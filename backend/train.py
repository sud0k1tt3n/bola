"""
Дообучение YOLO11n на 3 класса и оценка метрик для docs/TEST_PROTOCOL.md.

    python prepare_dataset.py экспорт_cvat.zip          # один раз: → data/hazards/
    python train.py train                               # обучение (~20–60 мин на CPU)
    python train.py eval                                # P/R/mAP по классам на val
    python train.py speed --images ../data/samples      # время обработки

После обучения лучшие веса копируются в models/best.pt (прежние — в
models/best.prev.pt) — backend подхватит их при следующем запуске.
Подробно — docs/TRAINING.md.
"""
from __future__ import annotations

import argparse
import shutil
import statistics
from pathlib import Path

import config

DEFAULT_DATA = config.PROJECT_DIR / "data" / "hazards" / "hazards.yaml"


def _check_data(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"Нет {p}. Сначала подготовьте датасет: python prepare_dataset.py <экспорт CVAT>")
    text = p.read_text(encoding="utf-8")
    missing = [c for c in config.CLASSES if c not in text]
    if missing:
        raise SystemExit(f"В {p} нет классов {missing}: backend ждёт ровно {list(config.CLASSES)}")
    if not any(line.startswith("path:") for line in text.splitlines()):
        # yaml переносимый (без абсолютного пути) — подставляем папку, где он лежит.
        local = config.BACKEND_DIR / "runs" / "hazards.resolved.yaml"
        local.parent.mkdir(exist_ok=True)
        local.write_text(f"path: {p.resolve().parent.as_posix()}\n" + text, encoding="utf-8")
        return str(local)
    return str(p)


def cmd_train(a):
    data = _check_data(a.data)
    from ultralytics import YOLO

    model = YOLO(a.weights)  # старт с COCO-весов (config.BASE_WEIGHTS, по умолчанию yolo11n.pt)
    aug = (
        # Только съёмка сверху: у кадра нет «верха», поворачиваем как угодно.
        dict(degrees=180, flipud=0.5, fliplr=0.5)
        if a.aerial else
        # Смешанные данные (есть наземные фото) — переворачивать вверх ногами нельзя.
        dict(degrees=10, flipud=0.0, fliplr=0.5)
    )
    model.train(
        data=data, epochs=a.epochs, imgsz=a.imgsz, batch=a.batch,
        patience=a.patience,       # остановиться, если val не растёт N эпох
        mosaic=1.0, close_mosaic=10, hsv_v=0.4, scale=0.5,
        project=str(config.BACKEND_DIR / "runs"), name="hazards", exist_ok=True,
        seed=0, plots=True,
        **({"device": a.device} if a.device else {}),  # по умолчанию ultralytics сам выберет GPU, если он есть
        **aug,
    )
    best = Path(model.trainer.best)
    config.MODELS_DIR.mkdir(exist_ok=True)
    if config.CUSTOM_WEIGHTS.exists():
        shutil.copy(config.CUSTOM_WEIGHTS, config.MODELS_DIR / "best.prev.pt")
    shutil.copy(best, config.CUSTOM_WEIGHTS)
    print(f"\nВеса скопированы в {config.CUSTOM_WEIGHTS}. Графики и метрики: {Path(best).parent.parent}")
    print("Перезапустите backend — он подхватит модель (в шапке пропадёт «Демо-режим»).")


def cmd_eval(a):
    data = _check_data(a.data)
    from ultralytics import YOLO

    model = YOLO(str(config.CUSTOM_WEIGHTS))
    m = model.val(data=data, imgsz=a.imgsz, split=a.split, conf=0.001, iou=0.6,
                  **({"device": a.device} if a.device else {}))
    print("\n| Класс | Precision | Recall | mAP@0.5 | mAP@0.5:0.95 |")
    print("|---|---|---|---|---|")
    for i, cls_id in enumerate(m.box.ap_class_index):
        p, r, ap50, ap = m.box.class_result(i)
        print(f"| {model.names[int(cls_id)]} | {p:.3f} | {r:.3f} | {ap50:.3f} | {ap:.3f} |")
    print(f"| **все** | {m.box.mp:.3f} | {m.box.mr:.3f} | {m.box.map50:.3f} | {m.box.map:.3f} |")


def cmd_speed(a):
    """Сквозное время pipeline (как в API), а не только forward-pass сети."""
    import logging

    from detector import HazardDetector
    from pipeline import AnalyzeError, analyze_image

    logging.disable(logging.WARNING)
    det = HazardDetector()
    times = []
    for p in sorted(Path(a.images).glob("*")):
        if p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        try:
            r = analyze_image(p.read_bytes(), det, lat=a.lat, lon=a.lon)
        except AnalyzeError as e:
            print(f"{p.name}: {e.status} {e.detail}")
            continue
        times.append(r["processing_ms"])
        print(f"{p.name:30s} {r['processing_ms']:6d} мс  {r['overall_status']:6s} детекций: {len(r['detections'])}")
    if times:
        print(f"\nрежим={det.mode} кадров={len(times)} среднее={statistics.mean(times):.0f} мс "
              f"медиана={statistics.median(times):.0f} мс макс={max(times)} мс")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("train", "eval"):
        s = sub.add_parser(name)
        s.add_argument("--data", default=str(DEFAULT_DATA))
        s.add_argument("--imgsz", type=int, default=config.IMGSZ)
        s.add_argument("--device", default=None, help="cpu, 0 (GPU), mps (Mac M1+); по умолчанию — авто")
    t = sub.choices["train"]
    t.add_argument("--epochs", type=int, default=150)
    t.add_argument("--patience", type=int, default=40)
    t.add_argument("--batch", type=int, default=16, help="мало памяти — поставьте 8")
    t.add_argument("--weights", default=config.BASE_WEIGHTS,
                   help="стартовые веса: yolo11n.pt (по умолчанию), yolo11s.pt, yolov8n.pt или models/best.pt — дообучить")
    t.add_argument("--aerial", action="store_true", help="в датасете только съёмка сверху")
    sub.choices["eval"].add_argument("--split", default="val")
    s = sub.add_parser("speed")
    s.add_argument("--images", required=True)
    s.add_argument("--lat", type=float, default=None, help="для снимков без GPS")
    s.add_argument("--lon", type=float, default=None)
    args = ap.parse_args()
    {"train": cmd_train, "eval": cmd_eval, "speed": cmd_speed}[args.cmd](args)
