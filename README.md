# АэроРоуд — выявление опасных явлений на дорогах (прототип)

Веб-приложение для автоматического выявления опасных явлений на дорогах и переправах
по аэрофотоснимкам с БАС. Снимок с EXIF-геоданными → YOLO11n → bbox пересчитываются
в WGS84 → результат поверх карты Leaflet с цветовой индикацией опасности.
Один процесс uvicorn отдаёт и API, и интерфейс — без Docker, nginx и сборщиков.

| Класс | Что это | Уровень | Цвет |
|---|---|---|---|
| `flooding` | подтопление | непроходимо | 🔴 `#d32f2f` |
| `bridge_collapse` | обрушение моста | непроходимо | 🔴 `#d32f2f` |
| `fallen_tree` | упавшее дерево | риск | 🟡 `#fbc02d` |
| — | детекций нет | норма | 🟢 `#388e3c` |

## Быстрый старт

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Откройте **http://localhost:8000/** — frontend раздаётся тем же сервером.
Можно и отдельно: `cd frontend && python -m http.server 5500` → http://localhost:5500
(API будет вызываться на `localhost:8000`, CORS разрешён для любых портов localhost).

Проверить без модели: `cd backend && python run_demo.py` — любое фото встанет на юг Москвы
с двумя тестовыми участками, красным и жёлтым (см. [docs/RUN.md](docs/RUN.md#демо-режим-проверка-без-модели)).

Обучить на своей разметке из CVAT: [docs/TRAINING.md](docs/TRAINING.md).

Тестовые снимки: `python data/make_samples.py` → `data/samples/`. Подробнее — [docs/RUN.md](docs/RUN.md).

Без `models/best.pt` сервер стартует на COCO-весах `yolo11n.pt` с классами-заглушками,
а без установленного ultralytics — в режиме `mock` (цветовая эвристика). В обоих случаях
интерфейс показывает жёлтую плашку «Демо-режим».

## Интерфейс

| Экран | Что там |
|---|---|
| **Карта** | снимки БАС поверх подложки (повёрнуты по курсу), полигоны участков с номерами, поиск по населённым пунктам, ручная привязка (в т.ч. кликом по карте), панель участка с кропом и вердиктом оператора |
| **Снимки** | сводка и таблица загрузок: геопривязка, что нашли, статус, время обработки |
| **Участки** | все найденные участки с фильтром «Непроходимо / Риск» |
| **Слои** | подложка и видимость слоёв, прозрачность снимков |
| **Настройки** | светлая/тёмная тема, порог отображения, предупреждения, адрес API |

Подложки: **Гибрид** (спутник + подписи и дороги, по умолчанию), **Спутник** (Esri World Imagery),
**Схема** (OpenStreetMap), **Контраст** (ч/б спутник, цветные участки видны лучше). Адреса тайлов —
`TILES` в `backend/config.py`; в закрытом контуре замените их на свой тайл-сервер.

Проекты выбираются и создаются в шапке. Снимки, участки, вердикты и настройки хранятся
в sqlite (`backend/data/aeroroad.db`, создаётся сам). Снимок без GPS не теряется: он
получает статус «Нужна привязка», панель ручной привязки открывается автоматически.

## Структура

```
road-hazard-detector/
├── backend/
│   ├── main.py            FastAPI: все /api/*, /static, раздача frontend
│   ├── pipeline.py        байты файла → JSON (без HTTP, удобно тестировать)
│   ├── services.py        запись результата в проект, DTO для интерфейса
│   ├── storage.py         sqlite: проекты, снимки, участки, вердикты, настройки
│   ├── settlements.py     поиск населённых пунктов, подпись «N км от …»
│   ├── georef.py          EXIF/XMP → footprint → bbox → WGS84
│   ├── detector.py        YOLO11n + fallback-режимы, пороги, severity
│   ├── schemas.py         pydantic-контракт ответа
│   ├── config.py          пути, пороги CLS_CONF, параметры геопривязки
│   ├── prepare_dataset.py экспорт CVAT (YOLO 1.1) → датасет из 3 классов
│   ├── train.py           дообучение, метрики, замер скорости
│   ├── train_colab.ipynb  то же в Google Colab на GPU
│   ├── models/            сюда положить best.pt
│   ├── static/uploads/    загруженные снимки и миниатюры детекций
│   ├── data/              sqlite и снимки, ждущие привязки (создаётся сам)
│   ├── tests/             pytest: геопривязка, конвейер, хранилище, API
│   └── requirements.txt
├── frontend/              index.html, style.css, api.js, app.js, assets/ (vanilla JS + Leaflet)
├── data/                  make_samples.py, samples/, hazards/ (готовый датасет), README.md
└── docs/                  APPROACH.md, TRAINING.md, TEST_PROTOCOL.md, RUN.md, example_response.json
```

## API

`POST /api/analyze` — `multipart/form-data`: `image` (JPG/PNG), опционально `lat`, `lon`,
`altitude` (м над землёй), `yaw` (° от севера по часовой). Заполненные поля перекрывают EXIF.

| Код | Когда |
|---|---|
| 200 | успешно |
| 400 | пустой файл / не изображение |
| 413 | файл больше `MAX_UPLOAD_MB` (40 МБ) |
| 415 | не JPG/PNG |
| 422 | нет координат ни в EXIF, ни в форме; в теле `missing: ["lat","lon"]` и `photo_id` отложенного снимка |

Дополнительные поля формы: `project_id`, `place` (подпись участка). В ответ добавлены
`photo` и `zones` — записи хранилища для интерфейса.

Остальные эндпоинты (интерфейс оператора, все JSON):

| Метод и путь | Назначение |
|---|---|
| `GET /api/config` | подложки, стартовый вид карты, классы и пороги |
| `GET/POST /api/projects`, `DELETE /api/projects/{id}` | проекты |
| `GET /api/photos?projectId=`, `GET/DELETE /api/photos/{id}` | снимки |
| `POST /api/photos/{id}/geo` `{lat, lon, yaw?, altitude?, place?}` | ручная привязка: снимок прогоняется заново |
| `GET /api/zones?projectId=` | найденные участки |
| `POST /api/zones/{id}/verify` `{verdict: confirmed\|false_positive}` | вердикт оператора |
| `GET/PATCH /api/settings` | порог отображения, предупреждения |
| `GET /api/settlements?q=` | поиск населённых пунктов |

Пример ответа (сокращён; полный — [docs/example_response.json](docs/example_response.json)):

```json
{
  "image_id": "2c880a94-fa69-44b9-9f8e-ed4c1c311b4a",
  "image_url": "/static/uploads/2c880a94-fa69-44b9-9f8e-ed4c1c311b4a.jpg",
  "image_width": 1600, "image_height": 1200,
  "footprint": {"type": "Polygon", "coordinates": [[
    [38.17295393, 54.83946043], [38.17525394, 54.83853298],
    [38.17404607, 54.83753957], [38.17174606, 54.83846702], [38.17295393, 54.83946043]]]},
  "detections": [{
    "class": "flooding", "label_ru": "Подтопление", "confidence": 0.6551, "severity": "red",
    "bbox_px": [492.5, 380.0, 1157.5, 862.5],
    "polygon_wgs84": {"type": "Polygon", "coordinates": [[
      [38.17327941, 54.83886037], [38.17423535, 54.8384749],
      [38.17374969, 54.83807547], [38.17279375, 54.83846094], [38.17327941, 54.83886037]]]},
    "center_wgs84": [38.17351455, 54.83846792],
    "area_m2": 4060.9,
    "crop_url": "/static/uploads/2c880a94-..._det0.jpg"
  }],
  "overall_status": "red",
  "processing_ms": 101,
  "georef": {"altitude_m": 120.0, "yaw_deg": 35.0, "ground_width_m": 180.0,
             "ground_height_m": 135.0, "gsd_cm_per_px": 11.25,
             "sources": {"position": "exif:GPS", "altitude": "xmp:RelativeAltitude",
                         "yaw": "xmp:GimbalYawDegree", "fov": "exif:FocalLengthIn35mmFilm"},
             "warnings": [], "...": "..."},
  "model": {"mode": "mock", "weights": null, "warning": "..."}
}
```

Пример получен на синтетическом снимке `01_flooding_dji.jpg` в режиме `mock`
(без нейросети) — он демонстрирует формат и геопривязку, а не качество детекции.

## Отличия от исходного ТЗ (осознанные)

1. **Нет GPS → 422, а не 400.** В ТЗ оба варианта; 422 точнее (файл валиден, не хватает данных),
   в теле ответа — список недостающих полей, фронтенд подсвечивает их.
2. **Поворот снимка — своим слоем, а не leaflet-rotate.** leaflet-rotate вращает всю карту,
   а не отдельный снимок. `RotatedImageOverlay` в `app.js` (~60 строк) растягивает `<img>`
   CSS-матрицей по трём углам footprint — те же углы, что использует backend, поэтому
   снимок и полигоны совпадают при любом yaw. Поворот сделан сразу, не отложен в TODO.
3. **shapely не нужен.** Footprint и полигоны bbox считаются замкнутыми формулами.
4. **Высота: сначала `RelativeAltitude` из XMP DJI**, потом `GPSAltitude` с предупреждением:
   в EXIF обычно высота над уровнем моря, а не над землёй, и footprint получится завышенным.
5. **Курс: `GimbalYawDegree` (XMP) → `GPSImgDirection` → `FlightYawDegree` → 0 с предупреждением.**
   Учитывается и EXIF Orientation (снимок разворачивается, курс корректируется).
6. **VFOV** считается по фактическому соотношению сторон снимка (36·H/W мм), а не по 24 мм —
   иначе для 4:3 кадров высота footprint ошибочна на ~11%.
7. **Доп. поля ответа:** `georef` (параметры, источники, предупреждения, углы для оверлея),
   `model` (режим работы), `area_m2`, `crop_url`, `label_ru`, размеры снимка.
