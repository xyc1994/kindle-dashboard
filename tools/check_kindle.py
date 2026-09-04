#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kindle 越狱状态检测工具（Windows）

用法：
    1. 用 USB 线把 Kindle 连到电脑，等待出现 Kindle 盘符
    2. 双击运行本文件，或在终端执行：python check_kindle.py
    3. 把输出完整复制给 Agent

本脚本只读，不会修改 Kindle 上的任何文件。
"""

import ctypes
import os
import string
import sys

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3


def list_drives():
    """枚举本机所有盘符。"""
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    result = []
    for letter in string.ascii_uppercase:
        if bitmask & 1:
            result.append(f"{letter}:\\")
        bitmask >>= 1
    return result


def drive_type(root):
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
    except Exception:
        return 0


def volume_label(root):
    buf = ctypes.create_unicode_buffer(1024)
    try:
        ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), buf, ctypes.sizeof(buf), None, None, None, None, 0
        )
    except Exception:
        return ""
    return buf.value or ""


def looks_like_kindle(root):
    """Kindle 根目录必然同时存在 documents 与 system。"""
    return os.path.isdir(os.path.join(root, "documents")) and os.path.isdir(
        os.path.join(root, "system")
    )


def find_kindle():
    candidates = []
    for root in list_drives():
        if not os.path.exists(root):
            continue
        if not looks_like_kindle(root):
            continue
        candidates.append(
            {
                "root": root,
                "type": drive_type(root),
                "label": volume_label(root),
            }
        )
    return candidates


def safe_listdir(path):
    try:
        return sorted(os.listdir(path))
    except Exception as exc:
        return [f"<读取失败: {exc}>"]


def section(title):
    print()
    print("=" * 58)
    print(title)
    print("=" * 58)


def report(cand):
    root = cand["root"]
    section(f"发现 Kindle：{root}（卷标：{cand['label'] or '无'}）")

    print("\n[1] 根目录内容")
    for name in safe_listdir(root):
        full = os.path.join(root, name)
        tag = "目录" if os.path.isdir(full) else "文件"
        print(f"    [{tag}] {name}")

    # 关键目录
    checks = {
        "documents": "Kindle 原生书库",
        "extensions": "KUAL 扩展目录（越狱后才有）",
        "mrpackages": "MRPI 安装包目录（越狱后才有）",
        "koreader": "KOReader 主程序",
    }
    print("\n[2] 关键目录检查")
    found = {}
    for name, desc in checks.items():
        exists = os.path.isdir(os.path.join(root, name))
        found[name] = exists
        print(f"    {'[有]' if exists else '[无]'} {name:<12} {desc}")

    # 已装扩展
    ext_dir = os.path.join(root, "extensions")
    if found["extensions"]:
        print("\n[3] 已安装的 KUAL 扩展")
        for name in safe_listdir(ext_dir):
            print(f"    - {name}")
        # FBInk 探测
        hit = [n for n in safe_listdir(ext_dir) if "fbink" in n.lower()]
        print(f"\n    FBInk 扩展：{'已安装 -> ' + ', '.join(hit) if hit else '未安装'}")

        # KUAL 本体探测
        hit = [n for n in safe_listdir(ext_dir) if "kual" in n.lower()]
        print(f"    KUAL 本体　：{'存在 -> ' + ', '.join(hit) if hit else '未发现'}")
    else:
        print("\n[3] 未发现 extensions 目录，KUAL 大概率未安装")

    # 根目录残留的 .bin
    bins = [n for n in safe_listdir(root) if n.lower().endswith(".bin")]
    print("\n[4] 根目录残留的 .bin 文件（装 Hotfix / KUAL 时留下的）")
    if bins:
        for n in bins:
            print(f"    - {n}")
    else:
        print("    无")

    # 结论
    section("初步判断")
    if found["extensions"] and found["mrpackages"]:
        print("越狱体系：WinterBreak + MRPI + KUAL（传统体系，不是 KPM）")
    elif found["extensions"] or found["mrpackages"]:
        print("越狱体系：部分组件存在，状态不完整，需要人工确认")
    else:
        print("越狱体系：未见 MRPI / KUAL 痕迹 —— 可能只做了越狱 + Hotfix，")
        print("          或者根本没连对盘。请先确认 Kindle 已弹出飞行模式之外的正常状态。")

    if found["koreader"]:
        print("KOReader：已安装 -> 可以用它内置的 SSH 拿 shell（端口 2222）")
    else:
        print("KOReader：未安装 -> 需要安装后才能拿到 SSH")


def main():
    print("Kindle 越狱状态检测（只读，不会改动设备）")
    candidates = find_kindle()

    if not candidates:
        section("未发现 Kindle")
        print("可能原因：")
        print("  1. Kindle 没插，或 USB 线只是充电线")
        print("  2. Kindle 仍停留在 KOReader 界面（此时不会挂载 U 盘）")
        print("  3. 连接后未等盘符出现就运行了本脚本")
        print("\n请确认资源管理器里能看到 Kindle 盘符后重试。")
        return 1

    for cand in candidates:
        report(cand)

    section("下一步")
    print("把上面所有输出复制给 Agent，用于确定 FBInk 的安装方案。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"\n脚本异常：{exc}")
        print("请把这行报错一起发给 Agent。")
        sys.exit(2)
