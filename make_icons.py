#!/usr/bin/env python3
"""Draws the remote's home-screen icons (web/icon-*.png) using only the standard library."""

import struct
import zlib
from pathlib import Path

WEB = Path(__file__).resolve().parent / "web"
BG = (11, 13, 18)
SCREEN = (31, 38, 53)
ACCENT = (79, 140, 255)


def in_round_rect(x, y, x0, y0, x1, y1, r):
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def in_triangle(x, y, a, b, c):
    def side(p, q):
        return (x - q[0]) * (p[1] - q[1]) - (p[0] - q[0]) * (y - q[1])
    d1, d2, d3 = side(a, b), side(b, c), side(c, a)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def pixel(u, v):
    """Colour at normalised coords (0..1). Content stays inside the maskable safe zone."""
    if in_round_rect(u, v, 0.22, 0.26, 0.78, 0.66, 0.06):
        if in_triangle(u, v, (0.44, 0.36), (0.44, 0.56), (0.60, 0.46)):
            return ACCENT
        return SCREEN if in_round_rect(u, v, 0.245, 0.285, 0.755, 0.635, 0.045) else ACCENT
    if in_round_rect(u, v, 0.38, 0.71, 0.62, 0.745, 0.017):  # stand
        return ACCENT
    return BG


def png(size, path):
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")
        for x in range(size):
            row += bytes(pixel((x + 0.5) / size, (y + 0.5) / size))
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")
    path.write_bytes(data)


if __name__ == "__main__":
    for size, name in [(192, "icon-192.png"), (512, "icon-512.png"), (180, "apple-touch-icon.png")]:
        png(size, WEB / name)
        print("wrote", name)
