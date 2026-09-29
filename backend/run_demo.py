"""
Запуск в демо-режиме одной командой (Windows / macOS / Linux):

    cd backend
    python run_demo.py            # затем http://localhost:8000/

Любое загруженное фото встаёт на юг Москвы, на нём рисуются два тестовых
участка — красный (подтопление) и жёлтый (упавшее дерево). Нейросеть не
нужна. Подробности и настройки — docs/RUN.md, раздел «Демо-режим».
"""
import os

import uvicorn

os.environ.setdefault("RHD_DEMO", "1")
# Демо-данные — в отдельной базе, чтобы не смешивать с настоящими проектами.
os.environ.setdefault("RHD_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data-demo"))

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=int(os.getenv("PORT", "8000")))
