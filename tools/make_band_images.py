"""Make the switch-advisor images from a snip of the demo band.

Take the snip in the app's light theme with `/switch-test demo` showing (example numbers only), around the band and the
empty prompt box under it. For sharper images, zoom the app to 200% first (Ctrl+= five times; Ctrl+0 goes back).

The script keeps only the band and the empty prompt box. It cuts the status line under the prompt box (it shows the
real conversation's size and your model settings) and anything above or beside them. It stops without writing
anything unless it finds exactly one band (a light grey panel with buttons along its bottom) with an empty prompt box
right under it, the same width, and nothing but faint shadow around them. It can't read the band's numbers, so look
at band.png before you publish it.

Writes, into mod/switch-advisor/assets/:
  band.png             the band and the empty prompt box on a card: 830 px wide from a 100% snip (GitHub's README
                       column), 1660 from a 200% one. Embed it with width="830".
  social-card.png      1200x630, for posts (the Open Graph size that LinkedIn, X and Slack previews use)
  social-preview.png   1280x640, for the GitHub repository's social preview (Settings > General > Social preview)

Usage: python tools/make_band_images.py <snip.png>        (keep snips in data/snips/, which git ignores)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else None
OUT = ROOT / "mod" / "switch-advisor" / "assets"
FONTS = Path("C:/Windows/Fonts")
BOLD, SEMIBOLD, REGULAR = ["segoeuib.ttf", "arialbd.ttf"], ["seguisb.ttf", "arialbd.ttf"], ["segoeui.ttf", "arial.ttf"]

LABEL = "SWITCH-ADVISOR  ·  A CLAUDE CODE PLUGIN"
HEADLINE = ("Back after an hour? Your prompt cache expired.", "See if a new conversation is cheaper.")  # a line each
SUBLINE = "For large conversations: compares both costs, flags what the handoff might miss, lets you choose. No model calls."

README_W = 830                     # GitHub's README column on a repository page, in CSS px
BAND_H = 112                       # the demo band's height in a 100% snip, px (more if its text wraps)
PROMPT_H = 46                      # the prompt box's height in a 100% snip, px: one line, so this sets the scale
PROMPT_R = 12                      # the prompt box's corner radius in a 100% snip, px
INK, MUTED, EDGE = (31, 30, 28), (98, 93, 84), (222, 220, 215)
BG_TOP, BG_BOTTOM = (250, 248, 243), (238, 234, 225)
SHADE = 24                         # the shadow's grey


def fail(msg: str) -> None:
    raise SystemExit(f"Nothing written: {msg}")


# ---- finding the band and the prompt box ------------------------------------------------------------------------

def runs(flags, min_len: int = 1, max_gap: int = 0) -> list[tuple[int, int]]:
    """Runs of True as (start, end exclusive), bridging gaps of up to max_gap, keeping those at least min_len long."""
    out, start, last = [], None, None
    for i, v in enumerate(flags):
        if v:
            if start is None:
                start = i
            elif i - last - 1 > max_gap:
                out.append((start, last + 1))
                start = i
            last = i
    if start is not None:
        out.append((start, last + 1))
    return [r for r in out if r[1] - r[0] >= min_len]


def longest(flags) -> tuple[int, int]:
    found = runs(flags)
    return max(found, key=lambda r: r[1] - r[0]) if found else (0, 0)


def panels(a: np.ndarray, bg: np.ndarray) -> list[tuple[np.ndarray, tuple[int, int, int, int]]]:
    """Wide panels filled with a light neutral grey other than the background: (fill, (x0, y0, x1, y1))."""
    H, W, _ = a.shape
    colours, counts = np.unique(a.reshape(-1, 3), axis=0, return_counts=True)
    found = []
    for i in np.argsort(-counts)[:12]:
        c = colours[i]
        if not (225 <= c.min() and c.max() <= 250 and c.max() - c.min() <= 3 and np.abs(c - bg).max() >= 5):
            continue
        fill = np.abs(a - c).max(axis=2) <= 3          # the band's bottom rows shade a level or two darker
        for ry0, ry1 in runs(fill.mean(axis=1) >= 0.15, min_len=60, max_gap=2):
            cx0, cx1 = longest(fill[ry0:ry1].mean(axis=0) >= 0.25)
            py0, py1 = longest(fill[ry0:ry1, cx0:cx1].mean(axis=1) >= 0.25)
            cx0, cx1 = longest(fill[ry0 + py0:ry0 + py1].mean(axis=0) >= 0.25)
            rect = (cx0, ry0 + py0, cx1, ry0 + py1)
            if not any(overlap(rect, r) > 0.5 for _, r in found):   # the same panel through a near shade
                found.append((c, rect))
    return found


def overlap(r1, r2) -> float:
    """The shared area as a share of the smaller rectangle."""
    w = min(r1[2], r2[2]) - max(r1[0], r2[0])
    h = min(r1[3], r2[3]) - max(r1[1], r2[1])
    small = min((r1[2] - r1[0]) * (r1[3] - r1[1]), (r2[2] - r2[0]) * (r2[3] - r2[1]))
    return max(0, w) * max(0, h) / small if small > 0 else 0.0


def check_band(a: np.ndarray, bg: np.ndarray, fill: np.ndarray, rect) -> str | None:
    """Why this panel isn't the demo band, or None if it is."""
    x0, y0, x1, y1 = rect
    h, w = y1 - y0, x1 - x0
    s = h / BAND_H
    if not 0.85 <= s <= 2.8:
        return f"a grey panel {h} px tall (the band is about {BAND_H} px at 100% zoom, {2 * BAND_H} at 200%)"
    if w < 4 * h:
        return f"a grey panel {w}x{h} px, too narrow for the band"
    # buttons along its bottom: at least two runs of a slightly darker neutral grey
    bottom = a[y0 + int(0.70 * h):y0 + int(0.95 * h), x0:x1]
    level = int(fill.max())
    darker = (level - bottom.max(axis=2) >= 6) & (level - bottom.min(axis=2) <= 20) & \
             (bottom.max(axis=2) - bottom.min(axis=2) <= 3)
    buttons = runs(darker.mean(axis=0) >= 0.4, min_len=round(40 * s), max_gap=round(2 * s))
    if len(buttons) < 2:
        return "a grey panel without the band's buttons along its bottom"
    # plain background just above it and beside it
    e = round(8 * s)
    ring = np.concatenate([a[max(0, y0 - e):y0 - 2, max(0, x0 - e):x1 + e].reshape(-1, 3),
                           a[y0:y1, max(0, x0 - e):x0 - 2].reshape(-1, 3),
                           a[y0:y1, x1 + 2:x1 + e].reshape(-1, 3)])
    if len(ring) and (np.abs(ring - bg).max(axis=1) > 12).mean() > 0.02:
        return "a grey panel with something right above or beside it"
    return None


def find_prompt(diff: np.ndarray, band: tuple[int, int, int, int], s: float):
    """The prompt box right under the band, the same width: (rect, None), or (None, why not)."""
    bx0, by0, bx1, by1 = band
    bw, bh = bx1 - bx0, by1 - by0
    H, W = diff.shape
    line = diff >= 20
    tol = round(4 * s)

    def border(y: int) -> bool:
        r0, r1 = longest(line[y, max(0, bx0 - tol):min(W, bx1 + tol)])
        return r1 - r0 >= 0.6 * bw

    top = next((y for y in range(by1, min(H, by1 + int(0.3 * bh))) if border(y)), None)
    if top is None:
        return None, "no prompt box right under the band"
    bottom = next((y for y in range(top + int(0.25 * bh), min(H, top + int(0.8 * bh))) if border(y)), None)
    if bottom is None:
        return None, "the prompt box under the band has no bottom edge in the snip"
    for _ in range(round(2 * s)):                    # its outline is at most 2 px thick at 100%: never more
        if bottom + 1 < H and border(bottom + 1):
            bottom += 1
    mid = (top + bottom) // 2
    left = np.where(line[mid, max(0, bx0 - tol):bx0 + tol + 1])[0]
    right = np.where(line[mid, bx1 - tol - 1:min(W, bx1 + tol)])[0]
    if not len(left) or not len(right):
        return None, "the prompt box under the band isn't the band's width"
    return (max(0, bx0 - tol) + int(left.min()), top, bx1 - tol - 1 + int(right.max()) + 1, bottom + 1), None


def check_prompt_empty(a: np.ndarray, diff: np.ndarray, rect, s: float) -> str | None:
    """None if the box holds only its +, its placeholder and its mic; else what's in it."""
    x0, y0, x1, y1 = rect
    inner = diff[y0 + round(3 * s):y1 - round(2 * s), x0:x1]
    dark = a[y0 + round(3 * s):y1 - round(2 * s), x0:x1].min(axis=2)
    xs = np.arange(x1 - x0)[None, :].repeat(inner.shape[0], axis=0)
    edge = (xs < 10 * s) | (xs >= (x1 - x0) - 10 * s)            # the outline's rounded corners
    plus = (xs >= 12 * s) & (xs < 34 * s)
    text = (xs >= 40 * s) & (xs < 200 * s)                       # the placeholder: never darker than its grey
    mic = (xs >= (x1 - x0) - 36 * s) & (xs < (x1 - x0) - 12 * s)
    ink = inner >= 20
    if (ink & edge & (dark < 200)).any() or (ink & ~(edge | plus | text | mic)).any():
        return "the prompt box isn't empty (something besides the + icon, the placeholder and the mic)"
    if (ink & text & (dark < 110)).any():
        return "the prompt box isn't empty (text darker than the placeholder)"
    return None


def band_and_prompt(snip: Image.Image) -> tuple[Image.Image, float, tuple[int, int, int, int]]:
    """The band and the empty prompt box, cropped with a little room for the soft shadow, turned grey, and faded at
    the edges. Returns the crop, its scale relative to a 100% (96 DPI) snip, and where it came from in the snip."""
    a = np.asarray(snip.convert("RGB")).astype(int)
    H, W, _ = a.shape
    light = a[(a.min(axis=2) >= 248) & (a.max(axis=2) - a.min(axis=2) <= 3)]
    if len(light) < 0.25 * H * W:
        fail("the snip needs the app's light theme, with mostly white around the band.")
    colours, counts = np.unique(light, axis=0, return_counts=True)
    bg = colours[counts.argmax()]
    diff = np.abs(a - bg).max(axis=2)

    found, reasons = [], []
    for fill, rect in panels(a, bg):
        why = check_band(a, bg, fill, rect)
        s = (rect[3] - rect[1]) / BAND_H
        if why is None:
            prompt, why = find_prompt(diff, rect, s)
        if why is None:
            why = check_prompt_empty(a, diff, prompt, (prompt[3] - prompt[1]) / PROMPT_H)
        if why is None:
            found.append((rect, prompt, s))
        else:
            reasons.append(why)
    if not found:
        fail(f"no demo band with an empty prompt box under it ({'; '.join(dict.fromkeys(reasons)) or 'no grey panel at all'}). "
             "Snip around the band shown by /switch-test demo and the empty prompt box under it.")
    if len(found) > 1:
        fail(f"{len(found)} bands in the snip. Snip just one.")
    band, prompt, _ = found[0]
    s = (prompt[3] - prompt[1]) / PROMPT_H
    print(f"  band x {band[0]}-{band[2]}, y {band[1]}-{band[3]} (scale {s:.2f}); "
          f"prompt box x {prompt[0]}-{prompt[2]}, y {prompt[1]}-{prompt[3]}")

    # crop: a little room around them for the soft shadow, the tail under the prompt box stopping at anything that
    # isn't faint shadow (the status line); then nothing but faint, colourless shadow may be inside it. Where the snip
    # is cut tighter than that room, the missing part is padded with background after the edge fades.
    e, faint = round(8 * s), 12
    want = (min(band[0], prompt[0]) - e, band[1] - e, max(band[2], prompt[2]) + e, prompt[3] + e)
    x0, x1, y0 = max(0, want[0]), min(W, want[2]), max(0, want[1])
    y1, tail_end = prompt[3], min(H, want[3])
    while y1 < tail_end and (diff[y1, x0:x1] <= faint).all():
        y1 += 1
    padding = (x0 - want[0], y0 - want[1], want[2] - x1, want[3] - y1 if y1 == H else 0)   # left, top, right, bottom
    inside = np.zeros((H, W), bool)
    for rx0, ry0, rx1, ry1 in (band, prompt):
        inside[max(0, ry0 - 1):ry1 + 1, max(0, rx0 - 1):rx1 + 1] = True
    rgb = a[y0:y1, x0:x1]
    outside = ~inside[y0:y1, x0:x1]
    foreign = outside & ((diff[y0:y1, x0:x1] > faint) | (rgb.max(axis=2) - rgb.min(axis=2) > 3))
    if foreign.any():
        ys, xs = np.where(foreign)
        fail(f"something besides the band and the prompt box is at x {x0 + xs.min()}-{x0 + xs.max()}, "
             f"y {y0 + ys.min()}-{y0 + ys.max()}. Snip again, closer around them.")

    # ClearType draws text with coloured subpixel fringes, which look wrong anywhere but on this screen; the band's own
    # colours are all neutral grey, so the mean of the three channels is the text's plain greyscale coverage
    grey = rgb.astype(float).mean(axis=2)
    # fade the outer edge into the background, so the crop through the soft shadow leaves no line; never the boxes
    h, w = grey.shape
    dy = np.minimum(np.arange(h), np.arange(h)[::-1])[:, None]
    dx = np.minimum(np.arange(w), np.arange(w)[::-1])[None, :]
    weight = np.clip(np.minimum(dy, dx) / max(1, e), 0, 1)
    weight[~outside] = 1.0
    base = float(bg.mean())
    out = np.round(base + (grey - base) * weight).astype(np.uint8)
    left, top, right, bottom = padding
    if any(padding):
        out = np.pad(out, ((top, bottom), (left, right)), constant_values=int(round(base)))
        print(f"  the snip is cut close: padded {left}/{top}/{right}/{bottom} px (left/top/right/bottom) with background")
    print(f"  crop x {x0}-{x1}, y {y0}-{y1} of the snip")
    return Image.fromarray(np.stack([out] * 3, axis=2), "RGB"), s, (x0, y0, x1, y1)


# ---- drawing ----------------------------------------------------------------------------------------------------

_KERN: dict[str, tuple[dict, dict, int]] = {}


class Face:
    """A font with its kerning pairs (Pillow without libraqm doesn't kern)."""

    def __init__(self, names: list[str], size: int):
        self.path = next((FONTS / n for n in names if (FONTS / n).exists()), None)
        self.font = ImageFont.truetype(str(self.path), size) if self.path else ImageFont.load_default(size)
        self.size = size
        key = str(self.path)
        if key not in _KERN:
            pairs, cmap, upm = {}, {}, 1000
            try:
                from fontTools.ttLib import TTFont
                tt = TTFont(key, lazy=True)
                upm, cmap = tt["head"].unitsPerEm, tt.getBestCmap()
                if "kern" in tt:
                    for t in tt["kern"].kernTables:
                        pairs.update(getattr(t, "kernTable", {}))
            except Exception:
                pass
            _KERN[key] = (pairs, cmap, upm)
        self.pairs, self.cmap, self.upm = _KERN[key]

    def kern(self, a: str, b: str) -> float:
        ga, gb = self.cmap.get(ord(a)), self.cmap.get(ord(b))
        return self.pairs.get((ga, gb), 0) * self.size / self.upm if ga and gb else 0.0

    def width(self, text: str, tracking: float = 0.0) -> float:
        return (sum(self.font.getlength(c) for c in text) + sum(self.kern(a, b) for a, b in zip(text, text[1:]))
                + tracking * max(0, len(text) - 1))

    def draw(self, d: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, fill, tracking: float = 0.0) -> None:
        x, y = xy
        for i, c in enumerate(text):
            if i:
                x += self.kern(text[i - 1], c)
            d.text((x, y), c, font=self.font, fill=fill)
            x += self.font.getlength(c) + tracking


def rounded_mask(size: tuple[int, int], radius: int, ss: int = 4) -> Image.Image:
    big = Image.new("L", (size[0] * ss, size[1] * ss), 0)
    ImageDraw.Draw(big).rounded_rectangle([0, 0, big.width - 1, big.height - 1], radius * ss, fill=255)
    return big.resize(size, Image.LANCZOS)


def draw_card(canvas: Image.Image, content: Image.Image, x: int, y: int, pad: int, radius: int, hair: int,
              drop: int, blur: int, alpha: int = 46) -> None:
    """`content` on a white rounded card with a hairline edge, its visible top-left corner at (x, y), and its soft
    shadow drawn on the canvas itself (so no edge of a smaller layer can cut it off)."""
    cw, ch = content.width + 2 * pad, content.height + 2 * pad
    if blur > 0:
        shadow = Image.new("L", canvas.size, 0)
        shadow.paste(rounded_mask((cw, ch), radius).point(lambda v: v * alpha // 255), (x, y + drop))
        shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
        canvas.alpha_composite(Image.merge("RGBA", (*[Image.new("L", canvas.size, SHADE)] * 3, shadow)))
    edge = Image.new("RGBA", (cw, ch), EDGE + (255,))
    edge.putalpha(rounded_mask((cw, ch), radius))
    canvas.alpha_composite(edge, (x, y))
    face = Image.new("RGBA", (cw - 2 * hair, ch - 2 * hair), (255, 255, 255, 255))
    face.putalpha(rounded_mask(face.size, max(1, radius - hair)))
    canvas.alpha_composite(face, (x + hair, y + hair))
    canvas.alpha_composite(content.convert("RGBA"), (x + pad, y + pad))


def resize(img: Image.Image, f: float) -> Image.Image:
    """Hamming when enlarging a 100% snip (bicubic and Lanczos both overshoot into light halos around the text),
    Lanczos when reducing a 200% one."""
    size = (round(img.width * f), round(img.height * f))
    return img.resize(size, Image.HAMMING if f > 1 else Image.LANCZOS)


def readme_image(crop: Image.Image, s: float) -> Image.Image:
    """The card, exactly README_W CSS px wide at the snip's scale, with its shadow inside the canvas."""
    target = round(README_W * s)
    pad, hair = round(8 * s), max(1, round(s))
    room = target - crop.width - 2 * pad                       # what's left across for the shadow
    f = 1.0
    if room < round(8 * s):                                    # a wider band than usual: shrink it to fit
        f = (target - 2 * pad - round(24 * s)) / crop.width
        crop = resize(crop, f)
        room = target - crop.width - 2 * pad
    radius = round((PROMPT_R + 8) * s * f) + pad              # nests around the prompt box's corners
    side = room // 2
    blur = max(1, side // 3)
    drop = min(round(2 * s), blur)
    top, bottom = 3 * blur - drop + 1, drop + 3 * blur + 1
    canvas = Image.new("RGBA", (target, top + crop.height + 2 * pad + bottom), (0, 0, 0, 0))
    draw_card(canvas, crop, side, top, pad, radius, hair, drop, blur, alpha=40)
    return canvas


def luminance(c) -> float:
    def lin(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(float(x)) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(c1, c2) -> float:
    hi, lo = sorted((luminance(c1), luminance(c2)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def wrap(text: str, face: Face, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if face.width(trial) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    return lines + [line] if line else lines


def social(crop: Image.Image, s: float, size: tuple[int, int]) -> Image.Image:
    """Label, two-line headline and one-line subline at the top (clear of the corner where X labels link cards),
    then the screenshot card across the text column."""
    W, H = size
    k = min(W / 1200, H / 630)
    m = round(60 * k)
    t = np.linspace(0, 1, H)[:, None, None]
    canvas = Image.fromarray((np.array(BG_TOP) * (1 - t) + np.array(BG_BOTTOM) * t).repeat(W, axis=1).astype(np.uint8), "RGB").convert("RGBA")
    bg_at = lambda y: tuple(np.array(BG_TOP) * (1 - y / H) + np.array(BG_BOTTOM) * (y / H))
    d = ImageDraw.Draw(canvas)

    label, head = Face(SEMIBOLD, round(21 * k)), Face(BOLD, round(46 * k))
    size_sub = round(23 * k)
    sub = Face(REGULAR, size_sub)
    while sub.width(SUBLINE) > W - 2 * m and size_sub > round(18 * k):
        size_sub -= 1
        sub = Face(REGULAR, size_sub)

    y = round(44 * k)
    label.draw(d, (m, y), LABEL, MUTED, tracking=1.6 * k)
    checks = [("label", MUTED, y)]
    y += round(42 * k)
    for line in (part for h in HEADLINE for part in wrap(h, head, W - 2 * m)):
        head.draw(d, (m, y), line, INK)
        checks.append(("headline", INK, y))
        y += round(56 * k)
    y += round(16 * k)
    for line in wrap(SUBLINE, sub, W - 2 * m):
        sub.draw(d, (m, y), line, MUTED)
        checks.append(("subline", MUTED, y))
        y += round(32 * k)
    text_bottom = y

    # the card: as wide as the text column; the height left over goes a little under half above it, the rest below
    pad, hair = round(12 * k), max(1, round(k))
    min_gap, min_bottom = round(26 * k), round(44 * k)
    f = (W - 2 * m - 2 * pad) / crop.width
    if text_bottom + min_gap + crop.height * f + 2 * pad + min_bottom > H:   # not enough height: narrower, centred
        f = (H - text_bottom - min_gap - min_bottom - 2 * pad) / crop.height
    shot = resize(crop, f)
    radius = round((PROMPT_R + 8) * s * f) + pad              # nests around the prompt box's corners
    cw, ch = shot.width + 2 * pad, shot.height + 2 * pad
    top = text_bottom + max(min_gap, round((H - text_bottom - ch) * 0.42))
    x = m if cw >= W - 2 * m - 1 else (W - cw) // 2
    draw_card(canvas, shot, x, top, pad, radius, hair, drop=round(6 * k), blur=round(16 * k))

    worst = min((contrast(c, bg_at(yy + 15 * k)), name) for name, c, yy in checks)
    print(f"  {W}x{H}: screenshot {f:.2f}x, subline {size_sub}px, gaps {top - text_bottom}/{H - top - ch}px, "
          f"lowest text contrast {worst[0]:.2f}:1 ({worst[1]})")
    if worst[0] < 4.5:
        fail(f"the {worst[1]} would be {worst[0]:.2f}:1 against its background, under 4.5:1.")
    return canvas.convert("RGB")


def main() -> None:
    if SRC is None:
        sys.exit("usage: python tools/make_band_images.py <snip.png>")
    print(f"snip: {SRC}")
    crop, s, _ = band_and_prompt(Image.open(SRC))
    readme = readme_image(crop, s)
    cards = {name: social(crop, s, size) for name, size in (("social-card.png", (1200, 630)), ("social-preview.png", (1280, 640)))}
    OUT.mkdir(parents=True, exist_ok=True)
    readme.save(OUT / "band.png", optimize=True)
    for name, img in cards.items():
        img.save(OUT / name, optimize=True)
    print(f"  band.png: {readme.width}x{readme.height} (embed with width=\"{README_W}\")")
    print(f"written to {OUT}. It can't read the band's numbers: look at band.png before you publish it.")


if __name__ == "__main__":
    main()
