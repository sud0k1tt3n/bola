# Как запустить

## Требования
Python 3.10+, ~2 ГБ на диске (torch CPU), любой современный браузер, интернет для
тайлов OpenStreetMap и Leaflet с unpkg.com.

## Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
# Необязательно, но экономит ~1.5 ГБ: CPU-сборка torch
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

При первом старте без `models/best.pt` ultralytics скачает `yolo11n.pt` (~5 МБ).
Проверка: `curl http://localhost:8000/api/health` → `{"status":"ok","model":{...}}`.
Документация API (Swagger): http://localhost:8000/docs

## Frontend

* Вариант 1 (проще): http://localhost:8000/ — отдаётся тем же uvicorn. Docker и nginx не нужны.
* Вариант 2: `cd frontend && python -m http.server 5500` → http://localhost:5500
* Вариант 3: открыть `frontend/index.html` двойным кликом (file://) — тоже работает.

Во 2–3 вариантах API ищется на `http://localhost:8000`. Другой адрес — в интерфейсе:
Настройки → Подключение (или `window.AERO_ROAD_API_BASE` в `index.html`).

Браузеру нужен интернет для Leaflet (unpkg.com), шрифта Inter (Google Fonts) и тайлов
подложки (Esri — спутник и подписи гибрида, OpenStreetMap — схема). Esri World Imagery
требует указания атрибуции (выводится в углу карты); для промышленной эксплуатации
проверьте условия Esri или поднимите свой тайл-сервер.

Данные интерфейса (проекты, снимки, участки, вердикты) лежат в `backend/data/aeroroad.db`.
Начать с чистого листа — остановить сервер и удалить `backend/data/` и файлы в
`backend/static/uploads/`.

## Демо-режим: проверка без модели

Нужен, чтобы проверить всю цепочку «загрузка → карта → участки → вердикт», пока нет
обученной `best.pt`. Любое фото (хоть с телефона, хоть скриншот) встаёт на **юг Москвы**
(Чертаново), и на нём всегда рисуются **два участка: красный** «Подтопление» (слева внизу
кадра) **и жёлтый** «Упавшее дерево» (справа вверху). В шапке горит «Демо-режим: тестовые участки».

```bash
cd backend
python run_demo.py                 # самый простой способ, любая ОС
```

или через переменную окружения при обычном запуске:

```bash
RHD_DEMO=1 uvicorn main:app --port 8000          # macOS / Linux
$env:RHD_DEMO=1; uvicorn main:app --port 8000    # Windows PowerShell
set RHD_DEMO=1 && uvicorn main:app --port 8000   # Windows cmd
```

Затем http://localhost:8000/ → перетащите в окно любые JPG/PNG.

| Переменная | По умолчанию в демо | Что делает |
|---|---|---|
| `RHD_DEMO` | — | `1` включает всё сразу |
| `RHD_DETECTOR=demo` | включён | только фиксированные участки, координаты — как обычно |
| `RHD_DEMO_LOCATION` | `55.6100,37.6040` | своя точка, например `59.94,30.31` |
| `RHD_DEMO_FORCE_LOCATION` | `1` | `0` — брать демо-точку, только если в фото нет GPS |

Как это устроено: на геопривязку и отрисовку демо не влияет — работает тот же код, что и с
моделью. Подменяются только два входа: координаты центра кадра (с разбросом до 600 м,
чтобы разные снимки не легли стопкой; один и тот же файл всегда встаёт в одно место) и
ответ детектора. Ручные координаты из формы по-прежнему главнее демо-точки. Высота без
EXIF — 100 м, курс — 0° (север), поэтому кадр на карте ≈ 150 × 110 м. `run_demo.py` пишет
данные в отдельную базу `backend/data-demo/`, настоящие проекты не засоряются.

Где что лежит, если нужно поменять участки: `DEMO_DETECTIONS` в `backend/config.py`
(класс, уверенность, доли кадра `[x1, y1, x2, y2]`).

## Проверка на тестовых снимках

```bash
python data/make_samples.py                       # создаст data/samples/*
curl -F image=@data/samples/01_flooding_dji.jpg http://localhost:8000/api/analyze
curl -F image=@data/samples/04_no_gps.jpg http://localhost:8000/api/analyze          # → 422
curl -F image=@data/samples/05_no_exif.png -F lat=55.75 -F lon=37.62 -F yaw=90 \
     -F altitude=100 http://localhost:8000/api/analyze
```

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `RHD_DETECTOR` | `auto` | `auto` / `custom` / `coco_stub` / `mock` / `demo` |
| `RHD_BASE_WEIGHTS` | `yolo11n.pt` | стартовые веса для обучения и заглушки: `yolo11s.pt`, `yolov8n.pt` |
| `RHD_DEVICE` | `cpu` | `cpu`, `0` (GPU) |
| `RHD_IMGSZ` | `640` | размер входа сети |
| `RHD_SENSOR_WIDTH_MM` | `13.2` | ширина сенсора, если в EXIF нет фокусного в 35-мм экв. |
| `RHD_DEFAULT_ALTITUDE_M` | `100` | высота, если её нет нигде |
| `RHD_MAX_UPLOAD_MB` | `40` | лимит размера файла |
| `RHD_DATA_DIR` | `backend/data` | где хранить sqlite и снимки без привязки |

## Тесты

```bash
cd backend && pytest -q
```
`test_georef.py`, `test_pipeline.py`, `test_services.py` и `test_demo.py` не требуют нейросети (мок-детектор);
`test_api.py` пропускается, если не установлен fastapi.

## Обучение своей модели

```bash
cd backend
python prepare_dataset.py экспорт_из_cvat.zip   # → data/hazards/ + REPORT.md
python train.py train                           # → models/best.pt
python train.py eval                            # таблица для TEST_PROTOCOL
```
Перезапустите uvicorn — `best.pt` подхватится автоматически (режим `custom`).
Подробно, с Colab и разбором текущей разметки — [TRAINING.md](TRAINING.md).
