"""Generate app/web/icon.png from scratch.

Kept in the repo so the icon can be regenerated or tweaked without a design tool.
Draws at 4x and downsamples, which gives clean edges without anti-aliasing support
in Pillow's polygon drawing.

    python tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 256
SS = 4  # supersampling factor
S = SIZE * SS

OUT = Path(__file__).resolve().parent.parent / "app" / "web" / "icon.png"

BG_TOP = (43, 27, 71)
BG_BOTTOM = (18, 10, 34)
PUMPKIN = (255, 117, 24)
PUMPKIN_HI = (255, 176, 58)
PUMPKIN_LO = (226, 86, 12)
FACE = (27, 16, 48)
GLOW = (255, 209, 102)
WAVE = (168, 85, 247)
STEM = (74, 222, 128)


def rounded_gradient_background(img: Image.Image) -> None:
    grad = Image.new("RGB", (1, S))
    for y in range(S):
        t = y / (S - 1)
        grad.putpixel((0, y), tuple(
            round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)
        ))
    grad = grad.resize((S, S))

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=56 * SS, fill=255)
    img.paste(grad, (0, 0), mask)


def draw_waves(img: Image.Image) -> None:
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    # Two concentric arc pairs sweeping up and out from behind the pumpkin.
    for radius, width, alpha in ((104, 9), (150, 9)) and (
        (104, 9 * SS, 130), (152, 9 * SS, 72)
    ):
        r = radius * SS
        box = [S // 2 - r, S // 2 - r + 30 * SS, S // 2 + r, S // 2 + r + 30 * SS]
        d.arc(box, start=196, end=246, fill=(*WAVE, alpha), width=width)
        d.arc(box, start=294, end=344, fill=(*WAVE, alpha), width=width)
    img.alpha_composite(layer)


def draw_glow(img: Image.Image) -> None:
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = S // 2, int(S * 0.63)
    for i in range(28, 0, -1):
        a = int(4 + i * 1.1)
        rx, ry = int(90 * SS * i / 28), int(74 * SS * i / 28)
        d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=(*PUMPKIN, a // 3))
    layer = layer.filter(ImageFilter.GaussianBlur(10 * SS // 2))
    img.alpha_composite(layer)


def draw_pumpkin(img: Image.Image) -> None:
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = S // 2, int(S * 0.625)
    rx, ry = int(82 * SS), int(68 * SS)

    # stem
    d.rounded_rectangle(
        [cx - 7 * SS, cy - ry - 26 * SS, cx + 7 * SS, cy - ry + 8 * SS],
        radius=5 * SS, fill=STEM,
    )

    # body, with a vertical gradient faked by stacked ellipses
    for i in range(ry, 0, -1):
        t = 1 - (i / ry)
        col = tuple(round(PUMPKIN_HI[j] + (PUMPKIN_LO[j] - PUMPKIN_HI[j]) * t) for j in range(3))
        d.ellipse([cx - rx, cy - i, cx + rx, cy + i], fill=col)
    d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=PUMPKIN_LO, width=2 * SS)

    # ribs
    for offset in (-52, -26, 26, 52):
        ox = int(offset * SS)
        rr = int((rx - abs(offset) * SS * 0.55))
        d.ellipse([cx + ox - rr // 4, cy - ry, cx + ox + rr // 4, cy + ry],
                  outline=(0, 0, 0, 26), width=2 * SS)

    img.alpha_composite(layer)


def draw_face(img: Image.Image) -> None:
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = S // 2, int(S * 0.625)

    def tri(points, fill):
        d.polygon([(cx + int(x * SS), cy + int(y * SS)) for x, y in points], fill=fill)

    # eyes
    tri([(-44, -20), (-14, -36), (-8, -4), (-40, 0)], FACE)
    tri([(44, -20), (14, -36), (8, -4), (40, 0)], FACE)
    # nose
    tri([(0, -8), (10, 8), (-10, 8)], FACE)
    # mouth
    d.polygon([(cx + int(x * SS), cy + int(y * SS)) for x, y in
               [(-46, 20), (-30, 18), (-24, 30), (-12, 20), (0, 32),
                (12, 20), (24, 30), (30, 18), (46, 20),
                (40, 46), (20, 54), (-20, 54), (-40, 46)]], fill=FACE)
    # teeth
    for x0, x1 in ((-34, -22), (-8, 6), (20, 32)):
        d.polygon([(cx + int(x0 * SS), cy + int(22 * SS)),
                   (cx + int(x1 * SS), cy + int(22 * SS)),
                   (cx + int(((x0 + x1) / 2) * SS), cy + int(36 * SS))], fill=PUMPKIN)

    img.alpha_composite(layer)

    # inner candle glow spilling from the eyes
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.polygon([(cx + int(x * SS), cy + int(y * SS)) for x, y in
                [(-40, -18), (-16, -32), (-11, -7), (-37, -3)]], fill=(*GLOW, 120))
    gd.polygon([(cx + int(x * SS), cy + int(y * SS)) for x, y in
                [(40, -18), (16, -32), (11, -7), (37, -3)]], fill=(*GLOW, 120))
    glow = glow.filter(ImageFilter.GaussianBlur(3 * SS))
    img.alpha_composite(glow)


def main() -> None:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    rounded_gradient_background(img)
    draw_waves(img)
    draw_glow(img)
    draw_pumpkin(img)
    draw_face(img)

    # Re-apply the rounded mask so nothing bleeds past the corners.
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=56 * SS, fill=255)
    img.putalpha(mask)

    final = img.resize((SIZE, SIZE), Image.LANCZOS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    final.save(OUT, "PNG", optimize=True)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
