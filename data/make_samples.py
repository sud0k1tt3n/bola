"""
Генератор СИНТЕТИЧЕСКИХ тестовых снимков с EXIF/XMP для проверки геопривязки
и сквозного прогона API. Это не реальные аэрофото: нейросеть по ним не
оценивается (для метрик нужны реальные размеченные кадры, см. README.md).

Запуск:  python data/make_samples.py   → data/samples/*.jpg|png
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from PIL.TiffImagePlugin import IFDRational

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "backend"))
from georef import deg_to_dms_rational  # noqa: E402

OUT = ROOT / "samples"
W, H = 1600, 1200  # 4:3, как у большинства камер DJI
rng = np.random.default_rng(42)


def terrain() -> Image.Image:
    """Поле + лес: зелёный шум с крупными пятнами."""
    base = rng.normal(0, 1, (H // 40, W // 40))
    big = np.array(Image.fromarray(((base - base.min()) / np.ptp(base) * 255).astype("uint8"))
                   .resize((W, H), Image.BICUBIC), dtype=float) / 255
    fine = rng.normal(0, 12, (H, W))
    r = 70 + 40 * big + fine
    g = 105 + 45 * big + fine
    b = 50 + 20 * big + fine
    return Image.fromarray(np.clip(np.dstack([r, g, b]), 0, 255).astype("uint8"))


def draw_road(img: Image.Image) -> None:
    d = ImageDraw.Draw(img)
    d.line([(0, 700), (W, 520)], fill=(120, 118, 112), width=70)
    d.line([(0, 700), (W, 520)], fill=(230, 230, 220), width=3)


def draw_water(img: Image.Image, bbox) -> None:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).ellipse(bbox, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(18))
    water = Image.new("RGB", img.size, (45, 95, 150))
    img.paste(water, (0, 0), mask)


def draw_fallen_tree(img: Image.Image) -> None:
    d = ImageDraw.Draw(img)
    d.line([(820, 520), (1080, 700)], fill=(92, 64, 40), width=22)
    for i in range(12):
        x = 850 + i * 20
        y = 540 + i * 14
        d.ellipse([x - 30, y - 22, x + 30, y + 22], fill=(40, 80, 35))


def exif_bytes(lat=None, lon=None, alt=None, direction=None, f_mm=None, f35=None, orientation=1):
    ex = Image.Exif()
    ex[0x0112] = orientation
    ex[0x0110] = "SYNTHETIC-TEST-CAM"
    gps = {}
    if lat is not None:
        gps.update({1: "N" if lat >= 0 else "S", 2: deg_to_dms_rational(lat),
                    3: "E" if lon >= 0 else "W", 4: deg_to_dms_rational(lon)})
    if alt is not None:
        gps.update({5: 0, 6: IFDRational(int(alt * 100), 100)})
    if direction is not None:
        gps.update({16: "T", 17: IFDRational(int(direction * 100), 100)})
    if gps:
        ex[0x8825] = gps
    exif_ifd = {}
    if f_mm:
        exif_ifd[0x920A] = IFDRational(int(f_mm * 100), 100)
    if f35:
        exif_ifd[0xA405] = int(f35)
    if exif_ifd:
        ex[0x8769] = exif_ifd
    return ex.tobytes()


def dji_xmp(rel_alt, gimbal_yaw, pitch=-90.0) -> bytes:
    return (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/" '
        f'drone-dji:RelativeAltitude="+{rel_alt:.2f}" '
        f'drone-dji:GimbalYawDegree="{gimbal_yaw:+.1f}" '
        f'drone-dji:FlightYawDegree="{gimbal_yaw:+.1f}" '
        f'drone-dji:GimbalPitchDegree="{pitch:+.1f}"/>'
        "</rdf:RDF></x:xmpmeta>"
    ).encode()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # 1. Подтопление дороги; DJI-подобные EXIF+XMP (Подмосковье, р. Ока)
    img = terrain(); draw_road(img); draw_water(img, (500, 380, 1150, 860))
    img.save(OUT / "01_flooding_dji.jpg", quality=92,
             exif=exif_bytes(54.8385, 38.1735, alt=215.3, f_mm=8.8, f35=24),
             xmp=dji_xmp(rel_alt=120.0, gimbal_yaw=35.0))

    # 2. Чистая дорога; только EXIF (курс в GPSImgDirection, фокус 35 мм экв.)
    img = terrain(); draw_road(img)
    img.save(OUT / "02_clear_road.jpg", quality=92,
             exif=exif_bytes(56.3269, 44.0059, alt=90.0, direction=300.0, f35=24))

    # 3. Упавшее дерево на дороге (мок-детектор его не увидит — нужна нейросеть)
    img = terrain(); draw_road(img); draw_fallen_tree(img)
    img.save(OUT / "03_fallen_tree.jpg", quality=92,
             exif=exif_bytes(59.9386, 30.3141, alt=80.0, direction=0.0, f35=24),
             xmp=dji_xmp(rel_alt=80.0, gimbal_yaw=0.0))

    # 4. Без GPS — API должен ответить 422 и попросить ввести координаты
    img = terrain(); draw_road(img); draw_water(img, (200, 200, 700, 600))
    img.save(OUT / "04_no_gps.jpg", quality=92, exif=exif_bytes(f35=24))

    # 5. PNG без EXIF — только с ручным вводом lat/lon/yaw/altitude
    img = terrain(); draw_road(img); draw_water(img, (900, 300, 1450, 800))
    img.save(OUT / "05_no_exif.png")

    for p in sorted(OUT.iterdir()):
        print(f"{p.name:24s} {p.stat().st_size / 1024:7.0f} КБ")


if __name__ == "__main__":
    main()
