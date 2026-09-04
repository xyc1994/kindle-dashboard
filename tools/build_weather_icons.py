# -*- coding: utf-8 -*-
"""
把 QWeather-Icons-1.8.0 的 SVG 图标栅格化成「黑色实心 + 透明底」灰度精灵图，
裁剪到内容包围盒，存放到 assets/weather_icons/<code>.png。

渲染器在运行时直接粘贴这些 PNG（用 alpha 作遮罩），不需要再依赖 SVG 渲染库。

依赖：resvg-py（已装在 venv）。
用法：python tools/build_weather_icons.py
"""

import io
import os

import resvg_py
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(
    ROOT, "QWeather-Icons-1.8.0", "QWeather-Icons-1.8.0", "icons"
)
OUT_DIR = os.path.join(ROOT, "assets", "weather_icons")

# 强制黑色填充（图标本身用 currentColor，墨水屏只需要黑色轮廓/实心）
CSS = "* { fill: #000000 !important; stroke: #000000 !important; }"

RENDER_SIZE = 128  # 高分辨率渲染，缩小后更清晰


def render_one(code: str, svg_path: str) -> Image.Image:
    png = resvg_py.svg_to_bytes(
        svg_path=svg_path,
        width=RENDER_SIZE,
        height=RENDER_SIZE,
        background=None,
        style_sheet=CSS,
    )
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    alpha = im.split()[-1]
    bbox = alpha.getbbox()
    if bbox:
        im = im.crop(bbox)
    return im


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    count = 0
    errors = []
    for fn in sorted(os.listdir(SRC_DIR)):
        if not fn.endswith(".svg"):
            continue
        code = fn[:-4]
        if code.endswith("-fill"):  # 跳过实心填充变体，保留描边版
            continue
        try:
            im = render_one(code, os.path.join(SRC_DIR, fn))
            im.save(os.path.join(OUT_DIR, f"{code}.png"))
            count += 1
        except Exception as e:  # noqa: BLE001
            errors.append((code, repr(e)[:120]))
    print(f"生成 {count} 个图标到 {OUT_DIR}")
    if errors:
        print(f"失败 {len(errors)} 个：")
        for code, err in errors[:20]:
            print("  ", code, err)


if __name__ == "__main__":
    main()
