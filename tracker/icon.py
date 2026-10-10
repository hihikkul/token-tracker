"""Tiny anti-aliased icon renderer (standard library only).

Draws the app mark — a dark rounded tile with a 270° gauge ring — as RGBA and encodes
it as PNG, so the widget can put the remaining limit on its taskbar button and
install can write a matching .ico without any image library.

    gauge_png(size, frac, color, text)   PNG bytes for one size
    write_ico(path)                      multi-size .ico of the static app icon
"""
import math
import struct
import zlib

TILE = (24, 24, 27)          # #18181b
TRACK = (58, 58, 64)
ACCENT = (232, 121, 74)      # warm clay, the static icon's arc
WHITE = (250, 250, 250)

# 3x5 digits, one string per row
DIGITS = {
    "0": ("111", "101", "101", "101", "111"), "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"), "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"), "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"), "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"), "9": ("111", "101", "111", "001", "111"),
    "!": ("010", "010", "010", "000", "010"), "-": ("000", "000", "111", "000", "000"),
}

START, SWEEP = 225.0, 270.0  # degrees clockwise from 12 o'clock; gap at the bottom


def _cov(d):
    """Signed distance (px, negative inside) -> pixel coverage."""
    return 0.0 if d >= 0.5 else 1.0 if d <= -0.5 else 0.5 - d


def _rrect(px, py, cx, cy, hw, hh, r):
    qx, qy = abs(px - cx) - hw + r, abs(py - cy) - hh + r
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r


def _arc(px, py, c, R, w, sweep):
    """Distance to a round-capped arc starting at START and running `sweep` degrees clockwise."""
    dx, dy = px - c, py - c
    if sweep <= 0:
        return 1e9
    th = math.degrees(math.atan2(dx, -dy)) % 360
    s = (th - START) % 360
    if s <= sweep:
        return abs(math.hypot(dx, dy) - R) - w / 2
    best = 1e9
    for a in (START, START + sweep):
        ar = math.radians(a)
        ex, ey = c + R * math.sin(ar), c - R * math.cos(ar)
        best = min(best, math.hypot(px - ex, py - ey) - w / 2)
    return best


def render(size, frac=0.72, color=ACCENT, text=None, bars=False, flat_bg=None, track=TRACK):
    """RGBA rows (list of bytearray) of the mark at `size` px."""
    n = size
    c = n / 2
    pad = n * 0.03
    radius = n * 0.22
    R = n * (0.355 if text else 0.31)
    w = max(1.8, n * (0.085 if text else 0.095))
    tile_col = TILE
    if flat_bg:  # bare ring on a solid square of the widget's background
        pad, radius, tile_col = -1.0, 0.0, flat_bg
        R, w = n * 0.38, max(1.6, n * 0.14)
    sweep = SWEEP * max(0.0, min(1.0, frac))
    glyphs = []
    if text:
        cell = max(1, round(n / 16))
        gw, gh = 3 * cell, 5 * cell
        gap = cell
        total = len(text) * gw + (len(text) - 1) * gap
        x0, y0 = round(c - total / 2), round(c - gh / 2)
        for i, ch in enumerate(text):
            pat = DIGITS.get(ch, DIGITS["-"])
            for ry, row in enumerate(pat):
                for rx, on in enumerate(row):
                    if on == "1":
                        gx = x0 + i * (gw + gap) + rx * cell
                        gy = y0 + ry * cell
                        glyphs.append((gx + cell / 2, gy + cell / 2, cell / 2))
    bar_rects = []
    if bars and not text:
        bw = n * 0.075
        for i, h in enumerate((0.16, 0.24, 0.32)):
            bx = c + (i - 1) * n * 0.12
            bar_rects.append((bx, c + n * 0.15 - h * n / 2, bw / 2, h * n / 2))

    ss = ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75))
    out = []
    for y in range(n):
        row = bytearray()
        for x in range(n):
            tile = arc = trk = fg = 0.0
            for ox, oy in ss:
                px, py = x + ox, y + oy
                tile += _cov(_rrect(px, py, c, c, c - pad, c - pad, radius) * 2) / 4
                a = _arc(px, py, c, R, w, sweep)
                arc += _cov(a * 2) / 4
                trk += _cov(_arc(px, py, c, R, w, SWEEP) * 2) / 4
                g = 0.0
                if any(abs(px - gx) <= h and abs(py - gy) <= h for gx, gy, h in glyphs):
                    g = 1.0
                for bx, by, hw, hh in bar_rects:
                    g = max(g, _cov(_rrect(px, py, bx, by, hw, hh, hw * 0.9) * 2))
                fg += g / 4
            # composite: tile -> track -> arc -> glyphs
            r, gch, b = tile_col
            for col, a in ((track, trk), (color, arc), (WHITE, fg)):
                r, gch, b = (r + (col[0] - r) * a, gch + (col[1] - gch) * a, b + (col[2] - b) * a)
            row += bytes((int(r + .5), int(gch + .5), int(b + .5), int(min(1.0, tile) * 255 + .5)))
        out.append(row)
    return out


def _in_gauge(px, py, c):
    th = math.degrees(math.atan2(px - c, -(py - c))) % 360
    return (th - START) % 360 <= SWEEP


def png(rows):
    h, w = len(rows), len(rows[0]) // 4
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def gauge_png(size, frac, color, text=None):
    return png(render(size, frac, color, text))


def ring_png(size, frac, color, bg, track):
    """Small gauge for the widget bar, drawn straight onto its background color."""
    return png(render(size, frac, color, flat_bg=bg, track=track))


def app_png(size):
    return png(render(size, 0.72, ACCENT, bars=True))


def write_ico(path, sizes=(16, 20, 24, 32, 40, 48, 64, 128, 256)):
    imgs = [app_png(s) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(imgs))
    off = 6 + 16 * len(imgs)
    dirs = b""
    for s, data in zip(sizes, imgs):
        dirs += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), off)
        off += len(data)
    with open(path, "wb") as f:
        f.write(head + dirs + b"".join(imgs))


def write_gauge_ico(path, key):
    if key is None:
        return write_ico(path, (16, 32, 48))
    left, source = key
    from .widget import ring_color, rgb
    sizes = (16, 32, 48)
    images = [gauge_png(size, left / 100, rgb(ring_color(source, 100 - left)),
                        str(left) if left < 100 else None) for size in sizes]
    offset = 6 + 16 * len(images)
    entries = []
    for size, data in zip(sizes, images):
        entries.append(struct.pack('<BBBBHHII', size, size, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    with open(path, 'wb') as stream:
        stream.write(struct.pack('<HHH', 0, 1, len(images)) + b''.join(entries) + b''.join(images))


if __name__ == "__main__":  # regenerate assets/: python -m tracker.icon
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "assets"
    write_ico(root / "token-tracker.ico")
    (root / "token-tracker.png").write_bytes(app_png(256))
    print("wrote", root)
