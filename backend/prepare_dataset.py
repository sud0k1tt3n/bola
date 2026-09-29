"""
Подготовка датасета из экспорта CVAT («YOLO 1.1») к обучению YOLO (ultralytics: YOLO11 / YOLOv8).

    python prepare_dataset.py путь/к/экспорту.zip          # или распакованная папка
    python train.py train --data ../data/hazards/hazards.yaml

Что делает:
  1. Читает obj.names и пары «картинка + .txt» (папку __MACOSX пропускает).
  2. Переводит классы разметки в 3 класса прототипа по CLASS_MAP (ниже).
     Классы без соответствия (normal_bridge, snow_drift, …) выбрасываются —
     их рамки убираются, а сама картинка остаётся.
  3. Картинки БЕЗ ЕДИНОЙ рамки по умолчанию НЕ берёт: в экспорте CVAT пустой
     .txt означает и «опасностей нет», и «ещё не разметили». Если такой кадр
     на самом деле с поваленным деревом, модель выучит «дерево = фон».
     Когда пустые кадры действительно проверены — добавьте --keep-empty.
  4. Делит на train / val (по умолчанию 80/20) так, чтобы почти одинаковые
     кадры попали в одну часть, а редкие классы были и там, и там.
  5. Пишет data/hazards/{images,labels}/{train,val}, hazards.yaml и отчёт
     REPORT.md со счётчиками и предупреждениями, плюс список неразмеченных
     кадров unlabeled.txt — чтобы доразметить их в CVAT.
"""
from __future__ import annotations

import argparse
import collections
import random
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from PIL import Image

import config

# Классы разметки → классы прототипа (config.CLASSES). None — выбросить рамку.
CLASS_MAP = {
    "flooded_road": "flooding",
    "flooded_area": "flooding",          # подтопление территории — тоже «красный»
    "collapsed_bridge": "bridge_collapse",
    "damaged_bridge": "bridge_collapse",  # осторожно: повреждение считаем непроходимым
    "fallen_tree_on_road": "fallen_tree",
    "fallen_tree_off_road": None,        # не на дороге — не опасность для проезда
    "normal_bridge": None,               # целый мост — фон (полезный «трудный негатив»)
    "snow_drift": None,                  # вне 3 классов ТЗ
    "ice_jam": None,
    # если выгрузка уже в наших именах — пропускаем как есть
    "flooding": "flooding", "bridge_collapse": "bridge_collapse", "fallen_tree": "fallen_tree",
}

IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def find_root(src: Path) -> Path:
    names = [p for p in src.rglob("obj.names") if "__MACOSX" not in p.parts]
    if not names:
        sys.exit(f"В {src} не найден obj.names — это точно экспорт CVAT в формате YOLO 1.1?")
    return names[0].parent


def read_pairs(root: Path):
    for img in sorted(root.rglob("*")):
        if img.suffix.lower() in IMG_EXT and "__MACOSX" not in img.parts and not img.name.startswith("._"):
            yield img, img.with_suffix(".txt")


def ahash(path: Path) -> int:
    """Грубый отпечаток картинки: близкие кадры (дубли, ресайзы) дают одинаковый хэш."""
    g = Image.open(path).convert("L").resize((16, 16))
    px = list(g.tobytes())
    if max(px) - min(px) < 8:          # однотонный кадр — не считаем дублем ничего
        return hash(path)
    mean = sum(px) / len(px)
    return sum(1 << i for i, v in enumerate(px) if v > mean)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="zip-архив или папка экспорта CVAT (YOLO 1.1)")
    ap.add_argument("--out", default=str(config.PROJECT_DIR / "data" / "hazards"))
    ap.add_argument("--val", type=float, default=0.2, help="доля валидации (0.2 = 20%%)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--keep-empty", action="store_true",
                    help="брать кадры без рамок как негативы (только если они проверены!)")
    ap.add_argument("--keep-off-road-trees", action="store_true",
                    help="считать fallen_tree_off_road тоже fallen_tree")
    args = ap.parse_args()

    cmap = dict(CLASS_MAP)
    if args.keep_off_road_trees:
        cmap["fallen_tree_off_road"] = "fallen_tree"
    target_ids = {name: i for i, name in enumerate(config.CLASSES)}

    src = Path(args.src)
    tmp = None
    if src.suffix.lower() == ".zip":
        tmp = tempfile.TemporaryDirectory()
        with zipfile.ZipFile(src) as z:
            z.extractall(tmp.name)
        src = Path(tmp.name)
    root = find_root(src)
    names = [l.strip() for l in (root / "obj.names").read_text(encoding="utf-8").splitlines() if l.strip()]
    unknown = [n for n in names if n not in cmap]
    if unknown:
        sys.exit(f"Нет правила для классов {unknown}: допишите их в CLASS_MAP в prepare_dataset.py")

    src_counts, dst_counts, dropped = collections.Counter(), collections.Counter(), collections.Counter()
    items, unlabeled, broken = [], [], []
    for img, txt in read_pairs(root):
        raw_lines = [l.split() for l in txt.read_text().splitlines() if l.strip()] if txt.exists() else []
        if not raw_lines:
            unlabeled.append(img)
            if not args.keep_empty:
                continue
        out_lines = []
        for parts in raw_lines:
            if len(parts) != 5:
                broken.append(f"{txt.name}: {' '.join(parts)}")
                continue
            name = names[int(parts[0])]
            src_counts[name] += 1
            target = cmap[name]
            if target is None:
                dropped[name] += 1
                continue
            x, y, w, h = (min(max(float(v), 0.0), 1.0) for v in parts[1:])
            if w <= 0 or h <= 0:
                broken.append(f"{txt.name}: нулевая рамка")
                continue
            out_lines.append(f"{target_ids[target]} {x:.6f} {y:.6f} {w:.6f} {h:.6f}")
            dst_counts[target] += 1
        items.append({"img": img, "lines": out_lines, "classes": {l.split()[0] for l in out_lines}})

    if not items:
        sys.exit("Не осталось ни одного кадра — проверьте разметку и CLASS_MAP")

    # --- разбиение: группы почти-дублей целиком в одну часть; редкие классы — в обе ---
    groups = collections.defaultdict(list)
    for it in items:
        groups[ahash(it["img"])].append(it)
    group_list = list(groups.values())
    rng = random.Random(args.seed)
    rng.shuffle(group_list)
    rarity = {str(i): dst_counts[c] for c, i in target_ids.items()}

    def key(g):  # группы с самым редким классом распределяем первыми
        cls = set().union(*(it["classes"] for it in g))
        return min((rarity[c] for c in cls), default=10**9)

    group_list.sort(key=key)
    val_target = max(1, round(len(items) * args.val))
    val, train = [], []
    val_by_cls, total_by_cls = collections.Counter(), collections.Counter()
    for g in group_list:
        for it in g:
            for c in it["classes"]:
                total_by_cls[c] += 1
    for g in group_list:
        cls = set().union(*(it["classes"] for it in g))
        # в val кладём, пока не набрали долю и пока класс в val не превысил свою долю
        want = len(val) + len(g) <= val_target and all(val_by_cls[c] < max(1, round(total_by_cls[c] * args.val)) for c in cls)
        (val if want else train).extend(g)
        if want:
            for it in g:
                for c in it["classes"]:
                    val_by_cls[c] += 1

    # --- запись ---
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    split_counts = {}
    for split, lst in (("train", train), ("val", val)):
        (out / "images" / split).mkdir(parents=True)
        (out / "labels" / split).mkdir(parents=True)
        c = collections.Counter()
        for it in lst:
            dst = out / "images" / split / it["img"].name
            shutil.copy2(it["img"], dst)
            (out / "labels" / split / (it["img"].stem + ".txt")).write_text(
                "\n".join(it["lines"]) + ("\n" if it["lines"] else ""))
            for l in it["lines"]:
                c[config.CLASSES[int(l.split()[0])]] += 1
        split_counts[split] = (len(lst), c)

    # Без абсолютного path: папку можно переносить (train.py подставит путь сам).
    yaml = ["# Датасет АэроРоуд — создан prepare_dataset.py", "train: images/train", "val: images/val", "", "names:"]
    yaml += [f"  {i}: {n}" for i, n in enumerate(config.CLASSES)]
    (out / "hazards.yaml").write_text("\n".join(yaml) + "\n", encoding="utf-8")
    (out / "unlabeled.txt").write_text("\n".join(p.name for p in unlabeled) + "\n", encoding="utf-8")

    # --- отчёт ---
    warn = []
    if unlabeled and not args.keep_empty:
        warn.append(f"{len(unlabeled)} кадров без разметки не взяты в обучение (список — unlabeled.txt). "
                    "Доразметьте их или, если опасностей там точно нет, запустите с --keep-empty.")
    for cls in config.CLASSES:
        n = dst_counts[cls]
        if n < 50:
            warn.append(f"Класс {cls}: всего {n} рамок — для устойчивого обучения нужно от ~150–200.")
        if split_counts["val"][1][cls] == 0 and n:
            warn.append(f"Класс {cls}: в валидацию не попало ни одного примера — метрика по нему не посчитается.")
    if broken:
        warn.append(f"Битых строк разметки: {len(broken)} (пропущены), первые: {broken[:3]}")

    lines = ["# Отчёт о подготовке датасета", "",
             f"Источник: `{args.src}`", "",
             "## Классы разметки → классы модели", "",
             "| В разметке | Рамок | Стало |", "|---|---|---|"]
    for n in names:
        lines.append(f"| {n} | {src_counts[n]} | {cmap[n] or '— выброшен'} |")
    lines += ["", "## Итог", "", "| Часть | Кадров | " + " | ".join(config.CLASSES) + " |",
              "|---|---|" + "---|" * len(config.CLASSES)]
    for split, (n, c) in split_counts.items():
        lines.append(f"| {split} | {n} | " + " | ".join(str(c[x]) for x in config.CLASSES) + " |")
    lines += ["", f"Кадров без разметки в экспорте: {len(unlabeled)} ({'взяты как негативы' if args.keep_empty else 'не взяты'}).",
              f"Групп почти-дублей (держатся в одной части): {sum(1 for g in group_list if len(g) > 1)}."]
    if warn:
        lines += ["", "## Предупреждения", ""] + [f"- {w}" for w in warn]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    print(f"\nГотово: {out}\nДальше:  python train.py train --data {out / 'hazards.yaml'}")
    if tmp:
        tmp.cleanup()


if __name__ == "__main__":
    main()
