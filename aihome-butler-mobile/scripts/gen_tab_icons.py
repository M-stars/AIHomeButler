#!/usr/bin/env python3
"""生成 uni-app tabBar 图标（81×81 PNG，透明底）。

形状用有向距离函数（SDF）在 4x 超采样网格上求值后降采样，
得到边缘平滑的纯色图标；不引入 Pillow 等依赖（标准库 zlib/struct 直写 PNG）。
运行：python scripts/gen_tab_icons.py
"""
import math
import struct
import zlib
from pathlib import Path

SIZE = 81
SS = 4  # 超采样倍数
OUT_DIR = Path(__file__).resolve().parent.parent / "src" / "static" / "tab"

GRAY = (148, 163, 184)   # 未选中（与 pages.json tabBar.color 一致）
BRAND = (56, 189, 248)   # 选中（brand-primary #38bdf8）


def circle(px, py, cx, cy, r):
    return math.hypot(px - cx, py - cy) - r


def rect(px, py, x0, y0, x1, y1, r=0.0):
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    hx = (x1 - x0) / 2 - r
    hy = (y1 - y0) / 2 - r
    qx = abs(px - cx) - hx
    qy = abs(py - cy) - hy
    outside = math.hypot(max(qx, 0), max(qy, 0))
    inside = min(max(qx, qy), 0)
    return outside + inside - r


def segment(px, py, ax, ay, bx, by, r):
    abx, aby = bx - ax, by - ay
    apx, apy = px - ax, py - ay
    t = max(0.0, min(1.0, (apx * abx + apy * aby) / (abx * abx + aby * aby + 1e-9)))
    return math.hypot(px - (ax + abx * t), py - (ay + aby * t)) - r


def union(*dists):
    return min(dists)


def subtract(d1, d2):
    return max(d1, -d2)


def sd_home(px, py):
    # 屋顶三角 + 房体 + 门
    roof = segment(px, py, 40.5, 12, 14, 38, 3.2) + segment(px, py, 40.5, 12, 67, 38, 3.2)
    body = rect(px, py, 20, 36, 61, 68, 3)
    door = rect(px, py, 34, 48, 47, 68, 2)
    return union(roof, subtract(body, door))


def sd_chat(px, py):
    # 圆角对话气泡 + 左下尾巴 + 三个点
    bubble = rect(px, py, 12, 16, 69, 56, 12)
    tail = segment(px, py, 24, 54, 20, 68, 3.2)
    dots = union(
        circle(px, py, 26, 36, 3.6),
        circle(px, py, 40.5, 36, 3.6),
        circle(px, py, 55, 36, 3.6),
    )
    return union(union(bubble, tail), dots)


def sd_automation(px, py):
    # 表盘圆环 + 时针分针（自动化 = 定时）
    ring = subtract(circle(px, py, 40.5, 40.5, 30), circle(px, py, 40.5, 40.5, 23))
    hand1 = segment(px, py, 40.5, 40.5, 40.5, 26, 3.0)
    hand2 = segment(px, py, 40.5, 40.5, 52, 47, 3.0)
    return union(ring, hand1, hand2)


def sd_settings(px, py):
    # 8 齿齿轮 + 中孔
    cx, cy = 40.5, 40.5
    ring = subtract(circle(px, py, cx, cy, 26), circle(px, py, cx, cy, 12))
    teeth = 1e9
    for i in range(8):
        ang = math.pi * 2 * i / 8
        bx = cx + math.cos(ang) * 26
        by = cy + math.sin(ang) * 26
        teeth = min(teeth, circle(px, py, bx, by, 6.5))
    return union(ring, teeth)


SHAPES = {
    "home": sd_home,
    "chat": sd_chat,
    "automation": sd_automation,
    "settings": sd_settings,
}


def sample(sd, px, py):
    d = sd(px, py)
    if d <= -0.8:
        return 255
    if d >= 0.8:
        return 0
    return int(max(0.0, min(1.0, (0.8 - d) / 1.6)) * 255)


def render(sd, rgb):
    alpha_grid = []
    for y in range(SIZE * SS):
        row = []
        py = (y + 0.5) / SS
        for x in range(SIZE * SS):
            px = (x + 0.5) / SS
            row.append(sample(sd, px, py))
        alpha_grid.append(row)
    # 超采样降级：块平均
    pixels = []
    for y in range(SIZE):
        for x in range(SIZE):
            acc = 0
            for sy in range(SS):
                for sx in range(SS):
                    acc += alpha_grid[y * SS + sy][x * SS + sx]
            a = acc // (SS * SS)
            pixels.append((rgb[0], rgb[1], rgb[2], a))
    return pixels


def write_png(path, pixels):
    raw = b""
    for y in range(SIZE):
        raw += b"\x00"  # filter type 0
        for x in range(SIZE):
            r, g, b, a = pixels[y * SIZE + x]
            raw += struct.pack("4B", r, g, b, a)

    def chunk(tag, data):
        payload = tag + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))

    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, sd in SHAPES.items():
        write_png(OUT_DIR / f"{name}.png", render(sd, GRAY))
        write_png(OUT_DIR / f"{name}-active.png", render(sd, BRAND))
        print(f"generated {name}.png / {name}-active.png")
    print(f"done -> {OUT_DIR}")


if __name__ == "__main__":
    main()
