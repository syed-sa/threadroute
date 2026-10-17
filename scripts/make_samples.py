"""Generate the three synthetic sample images used in the demo scenarios (re-run any time).

These are simple illustrations, not photos - for a real demo, replace them with real photos of a
damaged carton, a packing list and a returned garment (any png/jpg/webp works)."""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "samples"


def font(size: int):
    return ImageFont.load_default(size=size)


def damaged_carton() -> Image.Image:
    rng = random.Random(7)
    img = Image.new("RGB", (1000, 720), (178, 176, 170))
    d = ImageDraw.Draw(img, "RGBA")
    for y in range(480, 720, 6):  # warehouse floor lines
        d.line([(0, y), (1000, y)], fill=(160, 158, 152, 120), width=2)
    # carton A (front, crushed top-right corner + torn flap)
    d.polygon([(120, 250), (560, 250), (600, 330), (600, 640), (120, 640)], fill=(176, 130, 78), outline=(110, 78, 40))
    d.polygon([(120, 250), (330, 175), (560, 250)], fill=(160, 116, 66), outline=(110, 78, 40))
    d.polygon([(560, 250), (600, 330), (520, 360), (545, 300)], fill=(120, 84, 44))  # crushed corner
    d.polygon([(330, 175), (420, 205), (380, 235), (440, 262), (330, 250)], fill=(95, 64, 32))  # torn flap
    d.rectangle([(330, 175), (350, 640)], fill=(215, 205, 170, 170))  # tape
    for _ in range(14):  # water stains along the bottom
        x, y, r = rng.randint(130, 580), rng.randint(560, 640), rng.randint(14, 40)
        d.ellipse([x - r, y - r // 2, x + r, y + r // 2], fill=(70, 50, 28, 90))
    d.polygon([(120, 590), (200, 640), (120, 640)], fill=(110, 80, 44))  # dented bottom corner
    # label
    d.rectangle([(160, 400), (520, 560)], fill=(250, 250, 246), outline=(30, 30, 30), width=3)
    lines = [("AURELIO MENSWEAR", 26), ("SHIPMENT: SH-1002   PO: PO-5002", 22), ("DEST: ST-DEL-CP (Connaught Place)", 20),
             ("CARRIER: RoadRunner Express", 20), ("HANGING GARMENTS - DO NOT STACK", 20), ("Cartons: 3 of 3 | Units: 60", 20)]
    y = 412
    for text, size in lines:
        d.text((172, y), text, fill=(20, 20, 20), font=font(size))
        y += size + 8
    d.rectangle([(430, 300), (560, 340)], fill=(210, 40, 40))
    d.text((440, 305), "FRAGILE", fill=(255, 255, 255), font=font(26))
    # carton B behind, partly collapsed
    d.polygon([(640, 330), (940, 360), (930, 650), (650, 640)], fill=(168, 124, 74), outline=(110, 78, 40))
    d.polygon([(640, 330), (700, 300), (800, 330), (760, 360)], fill=(120, 84, 44))
    d.line([(650, 420), (930, 470)], fill=(95, 64, 32), width=5)  # crease where it was crushed
    d.text((680, 560), "SH-1002", fill=(40, 30, 20), font=font(24))
    return img


def packing_list() -> Image.Image:
    img = Image.new("RGB", (800, 1040), (250, 250, 247))
    d = ImageDraw.Draw(img)
    d.rectangle([(30, 30), (770, 1010)], outline=(40, 40, 40), width=3)
    d.text((60, 55), "AURELIO MENSWEAR", fill=(20, 20, 20), font=font(34))
    d.text((60, 105), "PACKING LIST", fill=(20, 20, 20), font=font(28))
    meta = ["Shipment:  SH-1003", "PO number: PO-5003", "From:      DC-BHW (Bhiwandi DC)", "To:        ST-BLR-IND (Indiranagar)",
            "Carrier:   AirLink Cargo (air)", "Packed on:  today"]
    y = 165
    for m in meta:
        d.text((60, y), m, fill=(30, 30, 30), font=font(24))
        y += 38
    y += 20
    d.line([(60, y), (740, y)], fill=(40, 40, 40), width=2)
    for x, h in ((70, "SKU"), (330, "Description"), (610, "Size"), (690, "Qty")):
        d.text((x, y + 8), h, fill=(20, 20, 20), font=font(22))
    y += 44
    d.line([(60, y), (740, y)], fill=(40, 40, 40), width=2)
    rows = [("SH-OXF-WHT", "Oxford Shirt - White", "L", "80"), ("SH-OXF-WHT", "Oxford Shirt - White", "M", "60")]
    for sku, desc, size, qty in rows:
        y += 14
        d.text((70, y), sku, fill=(30, 30, 30), font=font(22))
        d.text((330, y), desc, fill=(30, 30, 30), font=font(20))
        d.text((620, y), size, fill=(30, 30, 30), font=font(22))
        d.text((690, y), qty, fill=(30, 30, 30), font=font(22))
        y += 34
    y += 20
    d.line([(60, y), (740, y)], fill=(40, 40, 40), width=2)
    d.text((70, y + 14), "Total cartons: 7", fill=(20, 20, 20), font=font(24))
    d.text((70, y + 52), "TOTAL UNITS: 140", fill=(20, 20, 20), font=font(28))
    d.text((70, y + 130), "Packed by: R. Singh        Checked by: __________", fill=(60, 60, 60), font=font(20))
    d.text((70, y + 170), "Received by: __________    Date: ________", fill=(60, 60, 60), font=font(20))
    return img


def returned_jacket() -> Image.Image:
    img = Image.new("RGB", (800, 900), (232, 230, 224))
    d = ImageDraw.Draw(img, "RGBA")
    olive, dark = (92, 100, 58), (60, 66, 36)
    d.polygon([(250, 170), (550, 170), (600, 230), (640, 600), (560, 610), (545, 780), (255, 780), (240, 610), (160, 600), (200, 230)],
              fill=olive, outline=dark)  # body + sleeves
    d.polygon([(300, 140), (500, 140), (520, 200), (400, 250), (280, 200)], fill=dark)  # collar
    d.line([(400, 250), (400, 780)], fill=(200, 200, 190), width=5)  # zip
    d.rectangle([(240, 770), (560, 800)], fill=dark)  # ribbed hem
    d.rectangle([(150, 590), (250, 620)], fill=dark)
    d.rectangle([(550, 590), (650, 620)], fill=dark)
    for _ in range(3):  # beverage stain on chest
        d.ellipse([440 + _ * 14, 360 + _ * 10, 540 + _ * 14, 440 + _ * 10], fill=(96, 62, 30, 110))
    d.ellipse([470, 380, 520, 420], fill=(70, 42, 20, 150))
    d.rectangle([(300, 430), (360, 500)], fill=(70, 78, 44), outline=dark)  # pocket
    d.rectangle([(560, 640), (780, 760)], fill=(250, 250, 246), outline=(30, 30, 30), width=2)  # tag
    for i, t in enumerate(["RETURN RT-9001", "ORDER ORD-77120", "Bomber Jacket - Olive", "Size M", "Reason: too tight"]):
        d.text((570, 648 + i * 22), t, fill=(20, 20, 20), font=font(16))
    d.text((60, 40), "Returned garment - received at DC-GGN", fill=(50, 50, 50), font=font(26))
    return img


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, fn in (("damaged_carton_SH-1002.png", damaged_carton), ("packing_list_SH-1003.png", packing_list),
                     ("returned_jacket_RT-9001.png", returned_jacket)):
        fn().save(OUT / name)
        print("wrote", OUT / name)


if __name__ == "__main__":
    main()
