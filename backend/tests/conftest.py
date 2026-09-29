import sys
from pathlib import Path

# Модули backend импортируются как верхнеуровневые (так же, как их видит uvicorn).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
