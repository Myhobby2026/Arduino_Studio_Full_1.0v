#!/usr/bin/env python3
"""Generate the Arduino Studio window/application icon.

The repository deliberately ships no binary assets, so ``arduino.ico`` (used by
``arduino_studio.spec`` when building ``ArduinoStudio.exe`` and by the window
title bar at run time) is generated on demand by this script using nothing but
the standard library - no Pillow, no network, no image editor::

    python tools/make_icon.py                  # -> arduino_studio/resources/arduino.ico
    python tools/make_icon.py --png out.png    # also write a preview PNG
    python tools/make_icon.py --sizes 16 32 48 256

The mark is a teal rounded tile with the "board" glyph: a ring (a chip) with
two dots (the infinity eyes of the Arduino logo) and a serial pin bar below.
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import zlib
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "arduino_studio" / "resources" / "arduino.ico"
DEFAULT_SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)

#: RGBA parts of the palette (the teal is the Arduino brand teal).
TEAL = (0, 151, 157, 255)
TEAL_DARK = (0, 105, 112, 255)
WHITE = (255, 255, 255, 255)
TRANSPARENT = (0, 0, 0, 0)


def _smoothstep(edge0: float, edge1: float, value: float) -> float:
    """Antialiasing helper: 0 below *edge0*, 1 above *edge1*."""
    if edge1 == edge0:
        return 1.0 if value >= edge0 else 0.0
    t = max(0.0, min(1.0, (value - edge0) / (edge1 - edge0)))
    return t * t * (3.0 - 2.0 * t)


def _inside_rounded_square(x: float, y: float, size: float, radius: float, inset: float) -> float:
    """Coverage (0..1) of a rounded square, for antialiased drawing."""
    half = (size - inset * 2.0) / 2.0
    centre_x = centre_y = size / 2.0
    dx = abs(x - centre_x) - (half - radius)
    dy = abs(y - centre_y) - (half - radius)
    outside = math.hypot(max(dx, 0.0), max(dy, 0.0))
    inside = min(max(dx, dy), 0.0)
    distance = inside + outside
    return 1.0 - _smoothstep(-0.75, 0.75, distance - radius)


def draw(size: int) -> list[tuple[int, int, int, int]]:
    """Render one icon frame and return top-down RGBA pixels."""
    pixels: list[tuple[int, int, int, int]] = []
    radius = size * 0.22
    inset = size * 0.02
    cx = cy = size / 2.0
    ring_r = size * 0.29
    ring_w = max(1.2, size * 0.075)
    dot_r = max(1.1, size * 0.075)
    dot_dx = size * 0.135
    pin_y = size * 0.66
    pin_h = max(1.0, size * 0.055)
    pin_w = size * 0.36
    pin_count = 4
    pitch = pin_w / pin_count
    pin_tick = pitch * 0.62
    for y in range(size):
        for x in range(size):
            fx, fy = x + 0.5, y + 0.5
            tile = _inside_rounded_square(fx, fy, float(size), radius, inset)
            if tile <= 0.0:
                pixels.append(TRANSPARENT)
                continue
            # vertical gradient on the tile
            blend = _smoothstep(0.0, float(size), fy)
            base = tuple(
                int(round(a * (1.0 - 0.55 * blend) + b * 0.55 * blend)) for a, b in zip(TEAL, TEAL_DARK)
            )
            colour = (base[0], base[1], base[2], 255)
            distance = math.hypot(fx - cx, fy - cy)
            ring = 1.0 - _smoothstep(ring_w * 0.5 - 0.7, ring_w * 0.5 + 0.7, abs(distance - ring_r))
            if ring > 0.35 and distance < ring_r + ring_w:
                colour = _mix(colour, WHITE, ring)
            for sign in (-1.0, 1.0):
                dot_distance = math.hypot(fx - (cx + sign * dot_dx), fy - cy)
                cover = 1.0 - _smoothstep(dot_r - 0.7, dot_r + 0.7, dot_distance)
                if cover > 0.35:
                    colour = _mix(colour, WHITE, cover)
            if pin_y - pin_h <= fy <= pin_y + pin_h:  # header pins below the chip
                local = (fx - cx) + pin_w / 2.0
                if 0.0 <= local <= pin_w:
                    centre = (math.floor(local / pitch) + 0.5) * pitch
                    cover = 1.0 - _smoothstep(pin_tick / 2.0 - 0.7, pin_tick / 2.0 + 0.7, abs(local - centre))
                    if cover > 0.3:
                        colour = _mix(colour, WHITE, min(1.0, cover))
            alpha = 255 if tile >= 0.999 else int(255 * tile)
            pixels.append((colour[0], colour[1], colour[2], alpha))
    return pixels


def _mix(a: tuple[int, ...], b: tuple[int, ...], amount: float) -> tuple[int, ...]:
    """Blend two RGBA tuples."""
    return tuple(int(round(x * (1.0 - amount) + y * amount)) for x, y in zip(a[:3], b[:3])) + (a[3],)


def to_bgra_rows(pixels: list[tuple[int, int, int, int]], size: int) -> bytes:
    """Convert top-down RGBA pixels into the bottom-up BGRA rows an ICO needs."""
    out = bytearray()
    for row in range(size - 1, -1, -1):  # BITMAPINFOHEADER rows are bottom-up
        start = row * size
        for red, green, blue, alpha in pixels[start:start + size]:
            out += bytes((blue, green, red, alpha))
    return bytes(out)


def build_ico(sizes: tuple[int, ...]) -> bytes:
    """Assemble a multi-resolution ``.ico`` file (32 bit BMP frames + empty AND mask)."""
    entries: list[bytes] = []
    data: list[bytes] = []
    offset = 6 + 16 * len(sizes)
    for size in sizes:
        pixels = draw(size)
        image = to_bgra_rows(pixels, size)
        # 1 bit per pixel "transparency" mask, rows padded to 4 bytes; all zero
        # because the alpha channel of the BGRA data already carries the shape.
        row_bytes = ((size + 31) // 32) * 4
        and_mask = bytes(size * row_bytes)
        header = struct.pack(
            "<IiiHHIIiiII",
            40,            # BITMAPINFOHEADER size
            size,          # width
            size * 2,      # height (colour mask + 1 bit alpha mask)
            1,             # planes
            32,            # bits per pixel
            0,             # BI_RGB
            len(image) + len(and_mask),
            0, 0,          # pixels per metre (unused)
            0, 0,          # colours used / important
        )
        blob = header + image + and_mask
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset))
        data.append(blob)
        offset += len(blob)
    return struct.pack("<HHH", 0, 1, len(sizes)) + b"".join(entries) + b"".join(data)


def build_png(pixels: list[tuple[int, int, int, int]], size: int) -> bytes:
    """Write an RGBA PNG (used for the on-screen preview / Linux icon)."""
    raw = bytearray()
    for row in range(size):
        raw.append(0)  # filter type: none
        start = row * size
        for pixel in pixels[start:start + size]:
            raw += bytes(pixel)
    compressed = zlib.compress(bytes(raw), 9)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", compressed)
            + chunk(b"IEND", b""))


def main(argv: Optional[list[str]] = None) -> int:
    """CLI: render the frames and write the requested files."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT), help="path of the .ico to write")
    parser.add_argument("--png", default="", help="also write a preview PNG of the largest frame here")
    parser.add_argument("--sizes", nargs="*", type=int, default=list(DEFAULT_SIZES),
                        help=f"frame sizes (default: {' '.join(map(str, DEFAULT_SIZES))})")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    sizes = tuple(sorted({max(16, min(256, int(s))) for s in args.sizes})) or DEFAULT_SIZES
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(build_ico(sizes))
    print(f"wrote {output} ({output.stat().st_size:,} bytes, frames: {', '.join(map(str, sizes))})")

    if args.png:
        png_path = Path(args.png).expanduser()
        preview_size = min(128, max(sizes))
        png_path.parent.mkdir(parents=True, exist_ok=True)
        png_path.write_bytes(build_png(draw(preview_size), preview_size))
        print(f"wrote {png_path} ({png_path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
