#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 Kindle framebuffer 原始快照还原成 PNG，用于在电脑上客观验证上屏结果。

Kindle PW3 实测 framebuffer（eips -i）：
    xres=1072  yres=1448
    line_length=1088      ← 每行 1088 字节，只有前 1072 字节可见，尾部 16 字节是 padding
    bits_per_pixel=8  grayscale=1
所以一屏 = 1088 * 1448 = 1,575,424 字节，0=黑 255=白。

用法：
    python fb_raw_to_png.py shot1.raw                       # 自动推断尺寸
    python fb_raw_to_png.py shot1.raw -o out.png
    python fb_raw_to_png.py shot1.raw -W 1072 -H 1448 -S 1088
    python fb_raw_to_png.py kdashboard/                     # 批量转换目录内所有 .raw
"""

import argparse
import os
import struct
import sys
import zlib

DEFAULT_W = 1072
DEFAULT_H = 1448
DEFAULT_STRIDE = 1088


# ------------------------------------------------------------ PNG 输出
def _chunk(tag, data):
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def save_gray_png(path, w, h, rows):
    raw = bytearray()
    for r in rows:
        raw.append(0)
        raw.extend(r)
    out = b"\x89PNG\r\n\x1a\n"
    out += _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
    out += _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    out += _chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(out)
    return len(out)


def convert(src, dst, w=DEFAULT_W, h=DEFAULT_H, stride=DEFAULT_STRIDE):
    size = os.path.getsize(src)
    # 尺寸兜底：若文件长度与默认参数不符，按 stride 反推
    if size != stride * h:
        if size % h == 0:
            stride = size // h
        else:
            print(f"[跳过] {src}：{size} 字节，与 {w}x{h}@{stride} 不符")
            return None

    rows = []
    with open(src, "rb") as f:
        for _ in range(h):
            row = f.read(stride)
            if len(row) < w:
                print(f"[跳过] {src}：数据不足")
                return None
            rows.append(row[:w])

    n = save_gray_png(dst, w, h, rows)

    # 顺带做个内容体检，省得再单独看图
    flat = b"".join(bytes(r) for r in rows)
    black = sum(1 for b in flat if b < 64)
    white = sum(1 for b in flat if b > 200)
    total = len(flat)
    uniq = len(set(flat))
    print(
        f"  {os.path.basename(src)} -> {os.path.basename(dst)}  "
        f"({w}x{h}@{stride}, {n} 字节)\n"
        f"      黑像素 {black*100//total}%  白像素 {white*100//total}%  "
        f"灰阶种类 {uniq}"
    )
    return dst


def main():
    ap = argparse.ArgumentParser(description="Kindle framebuffer 快照 → PNG")
    ap.add_argument("src", help=".raw 文件或包含 .raw 的目录")
    ap.add_argument("-o", "--out", help="输出 PNG 路径（单文件模式）或输出目录")
    ap.add_argument("-W", type=int, default=DEFAULT_W, help=f"可见宽度，默认 {DEFAULT_W}")
    ap.add_argument("-H", type=int, default=DEFAULT_H, help=f"可见高度，默认 {DEFAULT_H}")
    ap.add_argument("-S", type=int, default=DEFAULT_STRIDE, help=f"行跨距，默认 {DEFAULT_STRIDE}")
    args = ap.parse_args()

    if os.path.isdir(args.src):
        outdir = args.out or args.src
        os.makedirs(outdir, exist_ok=True)
        raws = sorted(
            os.path.join(args.src, f) for f in os.listdir(args.src) if f.endswith(".raw")
        )
        if not raws:
            print(f"[提示] {args.src} 里没有 .raw 文件")
            return 1
        for r in raws:
            convert(r, os.path.join(outdir, os.path.basename(r)[:-4] + ".png"),
                    args.W, args.H, args.S)
        return 0

    dst = args.out or (os.path.splitext(args.src)[0] + ".png")
    if convert(args.src, dst, args.W, args.H, args.S) is None:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
