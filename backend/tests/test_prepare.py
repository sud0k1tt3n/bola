"""prepare_dataset.py на маленьком синтетическом экспорте CVAT (YOLO 1.1)."""
import subprocess
import sys
from pathlib import Path

from PIL import Image

BACKEND = Path(__file__).resolve().parents[1]
NAMES = ["collapsed_bridge", "damaged_bridge", "flooded_road", "flooded_area",
         "fallen_tree_on_road", "fallen_tree_off_road", "snow_drift", "ice_jam", "normal_bridge"]


def make_export(root: Path, n=20):
    d = root / "task 1" / "obj_Train_data" / "images"
    d.mkdir(parents=True)
    (root / "task 1" / "obj.names").write_text("\n".join(NAMES) + "\n")
    (root / "__MACOSX").mkdir()
    (root / "__MACOSX" / "._junk.jpg").write_bytes(b"x")
    for i in range(n):
        Image.new("RGB", (64, 64), (i * 12 % 255, 90, 200 - i * 5)).save(d / f"img_{i:03d}.jpg")
        cls = [2, 4, 5, 8, 0][i % 5] if i < 15 else None      # последние 5 — без разметки
        (d / f"img_{i:03d}.txt").write_text(f"{cls} 0.5 0.5 0.2 0.3\n" if cls is not None else "")


def run(args):
    return subprocess.run([sys.executable, "prepare_dataset.py", *args], cwd=BACKEND,
                          capture_output=True, text=True)


def test_prepare(tmp_path):
    make_export(tmp_path / "src")
    out = tmp_path / "out"
    r = run([str(tmp_path / "src"), "--out", str(out)])
    assert r.returncode == 0, r.stderr
    tr = list((out / "images" / "train").iterdir())
    va = list((out / "images" / "val").iterdir())
    assert len(tr) + len(va) == 15 and len(va) == 3          # пустые не взяты, 20% в val
    labels = [l for p in (out / "labels").rglob("*.txt") for l in p.read_text().split("\n") if l]
    ids = sorted({l.split()[0] for l in labels})
    assert ids == ["0", "1", "2"]                              # только 3 класса прототипа
    # off_road деревья и целые мосты выброшены: 3+3 из 15 кадров остаются с пустыми метками
    empty = [p for p in (out / "labels").rglob("*.txt") if not p.read_text().strip()]
    assert len(empty) == 6
    yaml = (out / "hazards.yaml").read_text()
    assert "path:" not in yaml and "2: fallen_tree" in yaml
    assert len((out / "unlabeled.txt").read_text().split()) == 5
    assert "bridge_collapse" in (out / "REPORT.md").read_text()


def test_keep_empty_and_off_road(tmp_path):
    make_export(tmp_path / "src")
    out = tmp_path / "out"
    r = run([str(tmp_path / "src"), "--out", str(out), "--keep-empty", "--keep-off-road-trees"])
    assert r.returncode == 0, r.stderr
    total = sum(1 for _ in (out / "images").rglob("*.jpg"))
    assert total == 20
    empty = [p for p in (out / "labels").rglob("*.txt") if not p.read_text().strip()]
    assert len(empty) == 5 + 3                                 # неразмеченные + целые мосты


def test_train_resolves_portable_yaml(tmp_path, monkeypatch):
    sys.path.insert(0, str(BACKEND))
    import config
    import train
    monkeypatch.setattr(config, "BACKEND_DIR", tmp_path)
    y = tmp_path / "ds" / "hazards.yaml"
    y.parent.mkdir()
    y.write_text("train: images/train\nval: images/val\nnames:\n  0: flooding\n  1: bridge_collapse\n  2: fallen_tree\n")
    resolved = Path(train._check_data(str(y)))
    assert resolved.read_text().startswith(f"path: {y.parent.resolve().as_posix()}")
