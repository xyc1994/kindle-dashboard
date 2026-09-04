#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成并部署 Kindle 端 KUAL 扩展（kdashboard）。

用法：
    python kindle_ext_builder.py --drive F:          # 生成 + 部署
    python kindle_ext_builder.py --drive F: --dry    # 只打印计划，不写入

产出（写入 <drive>/extensions/kdashboard/）：
    config.xml    KUAL 扩展声明
    menu.json     菜单
    diag.sh       只读诊断 → <drive>/kdashboard/diag.txt
    show.sh       图片上屏矩阵测试 → show.log + framebuffer 快照 *.raw
    part.sh       局部刷新定位测试
    suspend.sh    挂起-唤醒 60 秒测试
    clear.sh      清屏
    quit.sh       恢复 Kindle 原生界面
    dash.sh       看板主循环（取图 -> eips 画屏 -> rtc0 自动唤醒省电）
    start.sh      后台启动看板主循环
    stop.sh       设置停止标志
    t_gray8.png   1072x1448 灰度 PNG（colortype 0）
    strip_gray8.png   1072x200 局部刷新测试条

另外在 <drive>/dashboard/ 下写入：
    fl_intensity   前光默认亮度（0-24，由亮/暗菜单维护）
    server.conf    看板 server 配置（TOKEN 自动填入，HOST 需手填电脑内网 IP）

所有 .sh 强制 LF —— Kindle 的 busybox sh 不认 CRLF。

【路线说明 2026-09-04】
机上 /mnt/us/koreader/fbink 的特性串是
    FBInk 92e1270 for Kindle [..., Image=No, ...]
FBInk 的 -g/-i/--image 一组选项受 FBINK_WITH_IMAGE 编译开关控制，
未开启时整组选项不存在，传 -g 只会得到解析错误。
因此画图上屏改用设备自带的 eips：
    eips -g|-b image_path [-w waveform -f -x xpos -y ypos -v]
    -w gc16|gl16|du （默认 gc16）；-f 全刷，默认局部刷。
fbink 仍用于清屏（-k）与按区域触发刷新（-s）。

另外实测发现：eips 对 colortype=3（索引/调色板）PNG 支持不良，画面会花；
本扩展只生成 8bit 灰度 PNG（colortype=0），与实际渲染器保持一致。

【亮度 / 前光 2026-09-04】
PW3 前光 24 级（lipc 值 0-24），由 LIPC 控制：
    lipc-set-prop -i com.lab126.powerd flIntensity <0-24>
    lipc-get-prop  com.lab126.powerd flIntensity / flMaxIntensity
墨水屏本身不发光，"暗"通常是前光太弱。本扩展提供「亮度 +/-」菜单项，
实时改 lipc 值并持久化到 /mnt/us/dashboard/fl_intensity（dash.sh 每轮唤醒后重读应用）。

【RTC 自动唤醒节点 2026-09-04 修正】
旧固件用 /sys/devices/platform/mxc_rtc.0/wakeup_enable（kindle-dash 假设的路径），
在 PW3 5.12.x 上不存在。正确路径是标准 Linux 接口 /sys/class/rtc/rtc0/wakealarm：
    echo 0  > /sys/class/rtc/rtc0/wakealarm   # 清空
    echo +60 > /sys/class/rtc/rtc0/wakealarm   # 60 秒后唤醒
    echo mem > /sys/power/state
suspend.sh 会先探测节点是否存在（rtc0/wakealarm 优先，回退 mxc_rtc.0/wakeup_enable），
找不到则不挂起，避免设备卡死在休眠里。
"""

import argparse
import os
import struct
import sys
import zlib

EXT_NAME = "kdashboard"
TARGET_W = 1072
TARGET_H = 1448
STRIP_H = 200

FBINK = "/mnt/us/koreader/fbink"

# framebuffer 行跨距（eips -i: line_length=1088, xres=1072）
FB_STRIDE = 1088

# 前光默认亮度（lipc flIntensity 范围 0-24，25 级）
# 用户反馈原 20 偏暗，提到满级 24（墨水屏亮度=前光，想要更亮就拉满）
FL_DEFAULT = 24


# ---------------------------------------------------------------- PNG 写出
def _chunk(tag, data):
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def write_png(path, w, h, px, colortype=0):
    """px: 可迭代，产生 w*h 个 0..255 的灰度值（行优先）。

    colortype=0 → 8bit 灰度；colortype=3 → 8bit 索引 + 16 级灰阶调色板。
    """
    if colortype == 0:
        raw = bytearray()
        for y in range(h):
            raw.append(0)  # filter type: None
            raw.extend(px[y * w:(y + 1) * w])
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
        extra = b""
        body = bytes(raw)
    elif colortype == 3:
        # 16 级灰阶调色板
        pal = bytearray()
        for i in range(16):
            v = round(i * 255 / 15)
            pal += bytes((v, v, v))
        raw = bytearray()
        for y in range(h):
            raw.append(0)
            row = px[y * w:(y + 1) * w]
            raw.extend(bytes((v * 15 // 255) for v in row))
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 3, 0, 0, 0)
        extra = _chunk(b"PLTE", bytes(pal))
        body = bytes(raw)
    else:
        raise ValueError("colortype must be 0 or 3")

    out = b"\x89PNG\r\n\x1a\n"
    out += _chunk(b"IHDR", ihdr)
    out += extra
    out += _chunk(b"IDAT", zlib.compress(body, 9))
    out += _chunk(b"IEND", b"")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(out)
    return len(out)


# ---------------------------------------------------------------- 画面生成
def make_full_frame():
    """全屏测试画面：贴边框 + 标题条 + 16 级灰阶 + 网格 + 底部三格。"""
    w, h = TARGET_W, TARGET_H
    px = bytearray(b"\xff" * (w * h))

    def rect(x0, y0, x1, y1, v):
        x0, x1 = max(0, x0), min(w, x1)
        y0, y1 = max(0, y0), min(h, y1)
        for y in range(y0, y1):
            px[y * w + x0: y * w + x1] = bytes([v]) * (x1 - x0)

    # 贴边框 8px：验证分辨率与缩放是否 1:1
    rect(0, 0, w, 8, 0)
    rect(0, h - 8, w, h, 0)
    rect(0, 0, 8, h, 0)
    rect(w - 8, 0, w, h, 0)

    # 标题条：10px 竖条纹，验证横向缩放是否 1:1
    for i in range(0, (w - 16) // 10):
        if i % 2 == 0:
            rect(8 + i * 10, 60, 8 + (i + 1) * 10, 140, 0)
    rect(8, 60, w - 8, 64, 0)
    rect(8, 136, w - 8, 140, 0)

    # 16 级灰阶条：验证灰阶表现
    band_w = (w - 16) // 16
    for i in range(16):
        v = round(i * 255 / 15)
        rect(8 + i * band_w, 200, 8 + (i + 1) * band_w, 320, v)
    rect(8, 200, w - 8, 204, 0)
    rect(8, 316, w - 8, 320, 0)

    # 中部网格：验证几何是否变形
    for y in range(380, 1000, 40):
        rect(8, y, w - 8, y + 2, 170)
    for x in range(8, w - 8, 40):
        rect(x, 380, x + 2, 1000, 170)
    rect(60, 420, w - 60, 620, 220)

    # 定位十字：中心点，便于判断偏移
    cx, cy = w // 2, h // 2
    rect(cx - 2, cy - 60, cx + 2, cy + 60, 0)
    rect(cx - 60, cy - 2, cx + 60, cy + 2, 0)

    # 底部触摸区三格
    rect(0, 1200, w, 1380, 235)
    for i in range(3):
        x0 = i * w // 3
        x1 = (i + 1) * w // 3
        if i == 1:
            rect(x0, 1200, x1, 1380, 200)
        rect(x0, 1200, x0 + 3, 1380, 0)
    rect(0, 1200, w, 1204, 0)
    rect(0, 1376, w, 1380, 0)

    return px


def make_strip():
    """局部刷新测试条 1072x200：黑白强对比 + 左右不对称标记。"""
    w, h = TARGET_W, STRIP_H
    px = bytearray(b"\x00" * (w * h))

    def rect(x0, y0, x1, y1, v):
        x0, x1 = max(0, x0), min(w, x1)
        y0, y1 = max(0, y0), min(h, y1)
        for y in range(y0, y1):
            px[y * w + x0: y * w + x1] = bytes([v]) * (x1 - x0)

    rect(0, 0, w, 4, 255)
    rect(0, h - 4, w, h, 255)
    # 左端标记：3 个方块
    for i in range(3):
        rect(20 + i * 70, 40, 70 + i * 70, 160, 255)
    # 右端标记：1 个长条（不对称，便于判断水平翻转）
    rect(w - 260, 60, w - 20, 140, 255)
    # 中间：灰阶 4 级
    for i in range(4):
        rect(300 + i * 100, 50, 380 + i * 100, 150, 60 + i * 60)
    return px


# ---------------------------------------------------------------- 扩展文件
CONFIG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<extension>
\t<information>
\t\t<name>KDashboard</name>
\t\t<version>0.5</version>
\t\t<author>Life OS</author>
\t\t<id>KDashboard</id>
\t</information>
\t<menus>
\t\t<menu type="json" dynamic="true">menu.json</menu>
\t</menus>
</extension>
"""

MENU_JSON = """{
  "items": [
    {
      "name": "1. 图片上屏矩阵测试（推荐先跑这个）",
      "action": "sh /mnt/us/extensions/kdashboard/show.sh",
      "exitmenu": true,
      "refresh": false
    },
    {
      "name": "2. 局部刷新定位测试",
      "action": "sh /mnt/us/extensions/kdashboard/part.sh",
      "exitmenu": true,
      "refresh": false
    },
    {
      "name": "3. 运行诊断",
      "action": "sh /mnt/us/extensions/kdashboard/diag.sh",
      "exitmenu": true,
      "refresh": false
    },
    {
      "name": "4. 清屏",
      "action": "sh /mnt/us/extensions/kdashboard/clear.sh",
      "exitmenu": true,
      "refresh": false
    },
    {
      "name": "5. 亮度 +3（前光实时变亮，持久化）",
      "action": "sh /mnt/us/extensions/kdashboard/bright.sh +",
      "exitmenu": true,
      "refresh": false
    },
    {
      "name": "6. 亮度 -3（前光实时变暗，持久化）",
      "action": "sh /mnt/us/extensions/kdashboard/bright.sh -",
      "exitmenu": true,
      "refresh": false
    },
    {
      "name": "7. 挂起-唤醒 60 秒测试",
      "action": "sh /mnt/us/extensions/kdashboard/suspend.sh",
      "exitmenu": true,
      "refresh": false
    },
    {
      "name": "8. 启动看板主循环（后台常驻 + 自动省电）",
      "action": "sh /mnt/us/extensions/kdashboard/start.sh",
      "exitmenu": true,
      "refresh": false
    },
    {
      "name": "9. 停止看板（下次唤醒后生效）",
      "action": "sh /mnt/us/extensions/kdashboard/stop.sh",
      "exitmenu": true,
      "refresh": false
    },
    {
      "name": "10. 退出，回到 Kindle 原生界面",
      "action": "sh /mnt/us/extensions/kdashboard/quit.sh",
      "exitmenu": true,
      "refresh": false
    }
  ]
}
"""

DIAG_SH = """#!/bin/sh
# 只读诊断：结果落到 U 盘可见位置，无需 shell 即可取回
D=/mnt/us/extensions/kdashboard
OUT=/mnt/us/kdashboard/diag.txt
FB=""" + FBINK + """
mkdir -p /mnt/us/kdashboard

{
  echo "===== 生成时间 ====="
  date 2>&1

  echo ""
  echo "===== 内核 ====="
  uname -a 2>&1

  echo ""
  echo "===== 固件版本 ====="
  cat /etc/prettyversion.txt 2>&1

  echo ""
  echo "===== 屏幕信息 (eips -i) ====="
  eips -i 2>&1

  echo ""
  echo "===== eips 用法（无参数时打印）====="
  eips 2>&1

  echo ""
  echo "===== framebuffer 设备 ====="
  ls -l /dev/fb0 2>&1

  echo ""
  echo "===== FBInk 特性串 ====="
  "$FB" --help 2>&1 | grep -m1 'FBInk .* for Kindle'

  echo ""
  echo "===== 对照实验：fbink -g（预期失败，证明 Image=No）====="
  "$FB" -g file=$D/t_gray8.png > /tmp/fbg.txt 2>&1
  echo "fbink -g exit=$?"
  head -6 /tmp/fbg.txt 2>&1

  echo ""
  echo "===== 可用命令 ====="
  command -v eips 2>&1
  command -v dd 2>&1
  command -v wget 2>&1
  command -v curl 2>&1
  command -v lipc-set-prop 2>&1
  command -v dropbear 2>&1

  echo ""
  echo "===== 前光（亮度）====="
  lipc-get-prop com.lab126.powerd flIntensity 2>&1
  echo "--- flMaxIntensity ---"
  lipc-get-prop com.lab126.powerd flMaxIntensity 2>&1

  echo ""
  echo "===== 电量 ====="
  lipc-get-prop com.lab126.powerd battLevel 2>&1
  echo "--- charging ---"
  lipc-get-prop com.lab126.powerd isCharging 2>&1

  echo ""
  echo "===== 磁盘 ====="
  df -h /mnt/us 2>&1
} > "$OUT" 2>&1

exit 0
"""

SHOW_SH = """#!/bin/sh
# 图片上屏矩阵测试：eips 路线，每一步都抓一次 framebuffer 快照
# 说明：colortype=3（索引 PNG）在本机实测画面会花，因此只测 8bit 灰度 PNG。
D=/mnt/us/extensions/kdashboard
OUT=/mnt/us/kdashboard
LOG=$OUT/show.log
FB=""" + FBINK + """
STRIDE=""" + str(FB_STRIDE) + """
H=""" + str(TARGET_H) + """
mkdir -p $OUT
rm -f $OUT/shot*.raw

snap() {
  dd if=/dev/fb0 of=$OUT/$1 bs=$STRIDE count=$H 2>/dev/null
  echo "  [快照] $1  $(ls -l $OUT/$1 2>/dev/null | awk '{print $5}') 字节"
}

{
  echo "=== $(date) ==="
  echo "--- 停止 framework / 防息屏 ---"
  initctl stop framework 2>/dev/null
  initctl stop webreader 2>/dev/null
  lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>&1

  echo ""
  echo "--- 清屏 ---"
  eips -c 2>&1
  "$FB" -k 2>&1

  echo ""
  echo "===== [对照] fbink -g：预期失败，证明 Image=No ====="
  "$FB" -g file=$D/t_gray8.png > /tmp/fbg.txt 2>&1
  echo "exit=$?"
  head -4 /tmp/fbg.txt 2>&1

  echo ""
  echo "===== [1] eips -g 灰度PNG -w gc16 -f（全刷）====="
  eips -g $D/t_gray8.png -w gc16 -f 2>&1
  echo "exit=$?"
  snap shot1_gray8_gc16.raw

  echo ""
  echo "===== [2] eips -g 灰度PNG -w gl16（局部刷，无 -f）====="
  eips -g $D/t_gray8.png -w gl16 2>&1
  echo "exit=$?"
  snap shot2_gray8_gl16.raw

  echo ""
  echo "===== [3] eips -g 灰度PNG 不带任何波形参数（默认）====="
  eips -g $D/t_gray8.png 2>&1
  echo "exit=$?"
  snap shot3_gray8_default.raw

  echo ""
  echo "===== 完成 ====="
  ls -l $OUT/*.raw 2>&1
} > "$LOG" 2>&1

exit 0
"""

PART_SH = """#!/bin/sh
# 局部刷新定位测试：全屏底图 + 只刷中间一条，验证 -x/-y 定位精度
D=/mnt/us/extensions/kdashboard
OUT=/mnt/us/kdashboard
LOG=$OUT/part.log
FB=""" + FBINK + """
STRIDE=""" + str(FB_STRIDE) + """
H=""" + str(TARGET_H) + """
mkdir -p $OUT
rm -f $OUT/pshot*.raw

snap() {
  dd if=/dev/fb0 of=$OUT/$1 bs=$STRIDE count=$H 2>/dev/null
  echo "  [快照] $1  $(ls -l $OUT/$1 2>/dev/null | awk '{print $5}') 字节"
}

{
  echo "=== $(date) ==="
  echo "--- 停止 framework / 防息屏 ---"
  initctl stop framework 2>/dev/null
  initctl stop webreader 2>/dev/null
  lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>&1

  echo "--- 底图全刷 ---"
  eips -c 2>&1
  eips -g $D/t_gray8.png -w gc16 -f 2>&1
  echo "exit=$?"
  snap pshot1_base.raw

  echo ""
  echo "--- 局部刷新 strip @ (x=0, y=200) gl16 ---"
  eips -g $D/strip_gray8.png -x 0 -y 200 -w gl16 2>&1
  echo "exit=$?"
  snap pshot2_after_partial.raw

  echo ""
  echo "--- 再刷一条 strip @ (x=0, y=800) gl16 ---"
  eips -g $D/strip_gray8.png -x 0 -y 800 -w gl16 2>&1
  echo "exit=$?"
  snap pshot3_after_partial2.raw

  echo ""
  echo "--- 完成 ---"
} > "$LOG" 2>&1

exit 0
"""

SUSPEND_SH = """#!/bin/sh
# 挂起-唤醒 60 秒测试：验证 kindle-dash 式省电模型 + 前光应用
D=/mnt/us/extensions/kdashboard
OUT=/mnt/us/kdashboard
LOG=$OUT/suspend.log
mkdir -p $OUT

# 前光亮度：从持久化配置读，缺省 20（范围 0-24）
CFG=/mnt/us/dashboard/fl_intensity
DEF_FL=20
[ -f "$CFG" ] && FL=$(cat "$CFG" 2>/dev/null) || FL=$DEF_FL
case "$FL" in ''|*[!0-9]*) FL=$DEF_FL ;; esac
if [ "$FL" -lt 0 ]; then FL=0; fi
if [ "$FL" -gt 24 ]; then FL=24; fi

# 探测 RTC 唤醒节点（新固件 rtc0/wakealarm，旧固件 mxc_rtc.0/wakeup_enable）
WAKE=""
if [ -e /sys/class/rtc/rtc0/wakealarm ]; then
  WAKE=/sys/class/rtc/rtc0/wakealarm
elif [ -e /sys/devices/platform/mxc_rtc.0/wakeup_enable ]; then
  WAKE=/sys/devices/platform/mxc_rtc.0/wakeup_enable
fi

apply_fl() {
  lipc-set-prop -i com.lab126.powerd flIntensity "$FL" 2>&1
  echo "  [flIntensity=$FL] exit=$?"
}

{
  echo "=== $(date) ==="
  echo "--- 前光亮度 FL=$FL ---"
  initctl stop framework 2>/dev/null
  initctl stop webreader 2>/dev/null
  lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>&1
  apply_fl

  echo ""
  echo "--- RTC 唤醒节点：$WAKE ---"
  if [ -z "$WAKE" ]; then
    echo "未找到任何 RTC 唤醒节点！不挂起（避免设备卡死在休眠）"
  else
    echo "--- 画 sleep 画面（全刷）---"
    eips -g $D/t_gray8.png -w gc16 -f 2>&1
    echo "sleep draw exit=$?"

    echo ""
    echo "--- 设定 60 秒后唤醒并挂起 ---"
    if [ "$WAKE" = /sys/class/rtc/rtc0/wakealarm ]; then
      echo 0 > $WAKE 2>&1
      echo "+60" > $WAKE 2>&1
    else
      echo 60 > $WAKE 2>&1
    fi
    echo "wakealarm write exit=$?"
    echo mem > /sys/power/state 2>&1
    echo "resume exit=$?"

    echo ""
    echo "--- 唤醒后 ($(date))：重应用前光 + 画 strip 证明 eips 仍可用 ---"
    apply_fl
    eips -g $D/strip_gray8.png -x 0 -y 200 -w gl16 2>&1
    echo "wake draw exit=$?"
  fi

  echo ""
  echo "--- 恢复 framework ---"
  initctl start framework 2>/dev/null
  initctl start webreader 2>/dev/null
  lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>&1
} > "$LOG" 2>&1

exit 0
"""

BRIGHT_SH = """#!/bin/sh
# 前光亮度 ±3，范围 0-24，持久化到 /mnt/us/dashboard/fl_intensity
# 用法：bright.sh + | bright.sh -
CFG=/mnt/us/dashboard/fl_intensity
DEF=20; MIN=0; MAX=24; STEP=3
mkdir -p /mnt/us/dashboard
[ -f "$CFG" ] && val=$(cat "$CFG" 2>/dev/null) || val=$DEF
case "$val" in ''|*[!0-9]*) val=$DEF ;; esac
if [ "$1" = "+" ]; then val=$((val + STEP)); elif [ "$1" = "-" ]; then val=$((val - STEP)); fi
if [ "$val" -lt "$MIN" ]; then val=$MIN; fi
if [ "$val" -gt "$MAX" ]; then val=$MAX; fi
echo "$val" > "$CFG"
lipc-set-prop -i com.lab126.powerd flIntensity "$val" 2>&1
echo "flIntensity=$val (range $MIN-$MAX) exit=$?"
exit 0
"""

CLEAR_SH = """#!/bin/sh
export LD_LIBRARY_PATH=/mnt/us/koreader/libs:$LD_LIBRARY_PATH
""" + FBINK + """ -k 2>&1
eips -c 2>&1
exit 0
"""

QUIT_SH = """#!/bin/sh
# 恢复 Kindle 原生界面
lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>&1
eips -c 2>&1
initctl start framework 2>/dev/null
initctl start webreader 2>/dev/null
exit 0
"""

DASH_SH = """#!/bin/sh
# Kindle 看板主循环 ——「不停 framework + 保守重绘 + PC 自动发现」方案
# 思路：framework 始终运行（保留原生界面 / KUAL / 触屏 / 看其他书的能力），
#       每 REPAINT_INTERVAL 秒把缓存帧 eips 局部重绘一次压住原生主页重绘，
#       每 DAY_INTERVAL 秒重新拉一次新图（PC 端每次拉图实时重建）。
# 退出：touch /mnt/us/dashboard/stop 后下一轮检测到即退出，原生界面恢复、KUAL 可进。
#
# PC 地址自动发现（适配 PC 经常换 wifi / IP 变化）：
#   启动（及连续取图失败 3 次后）扫描本机网段，找响应正确 token 的 PC:8765，
#   缓存到 dashboard/pc_ip。server.conf 里的 HOST 作为优先缓存，可留空。
D=/mnt/us/extensions/kdashboard
CFG_DIR=/mnt/us/dashboard
CONF=$CFG_DIR/server.conf
LOG=$CFG_DIR/dash.log
FL_CFG=$CFG_DIR/fl_intensity
STOP=$CFG_DIR/stop
COUNT=$CFG_DIR/refresh_count
PC_IP_FILE=$CFG_DIR/pc_ip

# ---- 默认值 ----
HOST=""
PORT=8765
TOKEN=""
DAY_INTERVAL=60         # 拉新图间隔（秒）= 60 秒（PC 端每次拉图实时重建，时间误差<=60s）
REPAINT_INTERVAL=15     # 重绘缓存帧间隔（秒）= 15 秒（压住原生 UI 重绘，不停 framework）
NIGHT_INTERVAL=3600     # 夜间拉图间隔（秒）= 1 小时（屏仍常显，仅放慢内容更新省电）
NIGHT_START=1           # 夜间开始小时（含）
NIGHT_END=6             # 夜间结束小时（不含）
FULL_REFRESH_RATE=5     # 每 N 次拉图做 1 次全刷（防残影）
DEF_FL=24

# ---- 读取 server.conf（KEY=VALUE）----
if [ -f "$CONF" ]; then
  . "$CONF"
fi
# 兼容：server.conf 若用 INTERVAL 则映射到 DAY_INTERVAL
[ -n "${INTERVAL:-}" ] && DAY_INTERVAL=$INTERVAL

# ---- 前光 ----
FL=$DEF_FL
[ -f "$FL_CFG" ] && FL=$(cat "$FL_CFG" 2>/dev/null)
case "$FL" in ''|*[!0-9]*) FL=$DEF_FL ;; esac
[ "$FL" -lt 0 ] && FL=0
[ "$FL" -gt 24 ] && FL=24

apply_fl() {
  lipc-set-prop -i com.lab126.powerd flIntensity "$FL" 2>&1
}

log() {
  echo "$(date '+%m-%d %H:%M:%S') $*" >> "$LOG"
}

[ -z "$TOKEN" ] && { log "ERROR: server.conf 未配置 TOKEN"; exit 1; }

# ---- 电量读取与上报（供 SYS STATUS 面板显示 Kindle 剩余电量）----
get_batt() {
  local b=""
  for p in /sys/class/power_supply/*/capacity; do
    [ -r "$p" ] && b=$(cat "$p" 2>/dev/null) && [ -n "$b" ] && break
  done
  [ -z "$b" ] && b=$(lipc-get-prop com.lab126.powerd battLevel 2>/dev/null)
  echo "$b"
}

report_battery() {
  local b c
  b=$(get_batt)
  c=$(lipc-get-prop com.lab126.powerd isCharging 2>/dev/null)
  case "$c" in
    y|Y|true|TRUE|1) c=1 ;;
    *) c=0 ;;
  esac
  if command -v curl >/dev/null 2>&1; then
    curl -s -m 10 -o /dev/null "http://$PC_HOST:$PORT/report?t=$TOKEN&b=$b&c=$c" 2>/dev/null
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 10 -O /dev/null "http://$PC_HOST:$PORT/report?t=$TOKEN&b=$b&c=$c" 2>/dev/null
  fi
}

fetch() {
  if command -v curl >/dev/null 2>&1; then
    curl -s -m 30 -o /tmp/dash.png "$URL"; return $?
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 30 -O /tmp/dash.png "$URL"; return $?
  fi
  return 1
}

# ---- PC 地址自动发现 ----
# 扫描本机网段，找响应正确 token 的 PC:PORT，结果写入 PC_IP_FILE 缓存。
# 返回 0 表示发现成功（全局 PC_HOST 已设置），1 表示未发现。
discover_pc() {
  # 本机 IP（busybox ifconfig 格式 inet addr:IP；失败回退 ip addr）
  MYIP=$(ifconfig 2>/dev/null | grep -o 'inet addr:[0-9.]*' | grep -v '127.0.0.1' | head -1 | cut -d: -f2)
  [ -z "$MYIP" ] && MYIP=$(ip -4 addr 2>/dev/null | grep -o 'inet [0-9.]*' | grep -v '127.0.0.1' | head -1 | awk '{print $2}' | cut -d/ -f1)
  [ -z "$MYIP" ] && { log "discover: 无法获取本机 IP"; return 1; }
  NET=$(echo "$MYIP" | cut -d. -f1-3)
  log "discover: 本机 $MYIP 网段 $NET.0/24 扫描中..."

  # 候选：ARP 邻居中同网段 IP 优先（快），否则扫描整段
  CAND=$( (ip neigh show 2>/dev/null; cat /proc/net/arp 2>/dev/null) | awk -v net="$NET" '$1 ~ "^"net"[.]" {print $1}' | sort -u )
  if [ -z "$CAND" ]; then
    CAND=$(seq 1 254 | sed "s|^|$NET.|")
  fi

  # 并行探测开放 PORT 的主机（xargs -P；busybox 1.3x 支持，旧版回退串行）
  if echo | xargs -P 2 >/dev/null 2>&1; then PAR="-P 16"; else PAR=""; fi
  OPEN=$(echo "$CAND" | xargs $PAR -I{} sh -c 'code=$(curl -s -m1 -o /dev/null -w "%{http_code}" "http://{}:'$PORT'/health" 2>/dev/null); [ "$code" = "200" ] && echo {}' 2>/dev/null)

  # 对开放 PORT 的主机校验 token（/report?t=TOKEN 返回 200 即正确 PC）
  FOUND=""
  for ip in $OPEN; do
    code=$(curl -s -m2 -o /dev/null -w "%{http_code}" "http://$ip:$PORT/report?t=$TOKEN" 2>/dev/null)
    [ "$code" = "200" ] && { FOUND=$ip; break; }
  done

  if [ -n "$FOUND" ]; then
    echo "$FOUND" > "$PC_IP_FILE"
    log "discover: 发现 PC @ $FOUND"
    PC_HOST="$FOUND"
    return 0
  fi
  log "discover: 未发现 PC（确认 PC 服务端已启动且 Kindle 与 PC 同 wifi）"
  return 1
}

paint_full() {
  [ -s /tmp/dash.png ] || return 1
  eips -g /tmp/dash.png -w gc16 -f 2>>"$LOG" || eips -g /tmp/dash.png 2>>"$LOG" || true
}

paint_light() {
  [ -s /tmp/dash.png ] || return 1
  eips -g /tmp/dash.png -w gl16 2>>"$LOG" || true
}

# ---- 解析 PC 地址（自动发现，适配 PC 常换 wifi）----
PC_HOST=""
# 1) 缓存文件
if [ -f "$PC_IP_FILE" ]; then
  ip=$(cat "$PC_IP_FILE" 2>/dev/null)
  [ -n "$ip" ] && curl -s -m 3 -o /dev/null "http://$ip:$PORT/health" 2>/dev/null && PC_HOST="$ip"
fi
# 2) server.conf 里写的 HOST（若有且可达）
if [ -z "$PC_HOST" ] && [ -n "$HOST" ]; then
  curl -s -m 3 -o /dev/null "http://$HOST:$PORT/health" 2>/dev/null && PC_HOST="$HOST"
fi
# 3) 扫描发现
if [ -z "$PC_HOST" ]; then
  discover_pc || true
fi
if [ -z "$PC_HOST" ]; then
  log "ERROR: 未发现 PC，确认服务端已启动且 Kindle 与 PC 同 wifi"
  exit 1
fi
URL="http://$PC_HOST:$PORT/dash.png?t=$TOKEN"

# ---- 计数（跨重启持久化）----
N=0
[ -f "$COUNT" ] && N=$(cat "$COUNT" 2>/dev/null)
case "$N" in ''|*[!0-9]*) N=0 ;; esac

# ---- 启动：仅防息屏 + 应用前光（不停 framework）----
lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>&1
apply_fl
report_battery
log "看板主循环启动(不停 framework) PC=$PC_HOST PORT=$PORT FL=$FL"

last_fetch=0
fail_cnt=0
while true; do
  # 停止检查（最多延迟一个重绘周期生效）
  if [ -f "$STOP" ]; then
    log "检测到 stop 标志，退出主循环（framework 未停，原生界面保留）"
    rm -f "$STOP"
    lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>&1
    exit 0
  fi

  sleep "$REPAINT_INTERVAL"

  # 夜间放慢拉图间隔（重绘仍按 REPAINT_INTERVAL，屏常显压原生 UI）
  hr=$(date +%H)
  if [ "$hr" -ge "$NIGHT_START" ] && [ "$hr" -lt "$NIGHT_END" ]; then
    eff_interval=$NIGHT_INTERVAL
  else
    eff_interval=$DAY_INTERVAL
  fi

  now=$(date +%s)
  do_fetch=0
  if [ "$last_fetch" -eq 0 ] || [ $((now - last_fetch)) -ge "$eff_interval" ]; then
    do_fetch=1
  fi

  if [ "$do_fetch" -eq 1 ]; then
    if fetch; then
      fail_cnt=0
      report_battery
      N=$((N + 1))
      echo "$N" > "$COUNT"
      if [ $((N % FULL_REFRESH_RATE)) -eq 0 ]; then
        paint_full
        log "draw FULL (#$N)"
      else
        paint_light
        log "draw partial (#$N)"
      fi
      last_fetch=$now
    else
      log "WARN: 取图失败，重绘缓存帧"
      paint_light
      fail_cnt=$((fail_cnt + 1))
      # 连续失败 3 次：重新发现 PC（适配 PC 换 wifi / IP 变化）
      if [ "$fail_cnt" -ge 3 ]; then
        log "连续失败 $fail_cnt 次，重新发现 PC..."
        discover_pc || true
        if [ -n "$PC_HOST" ]; then
          URL="http://$PC_HOST:$PORT/dash.png?t=$TOKEN"
          fail_cnt=0
          log "已更新 PC 地址为 $PC_HOST"
        fi
      fi
    fi
  else
    # 未到拉图时机：仅重绘缓存帧压住原生 UI 重绘
    paint_light
  fi
done
"""

START_SH = """#!/bin/sh
# 启动看板主循环（后台常驻）
D=/mnt/us/extensions/kdashboard
rm -f /mnt/us/dashboard/stop
initctl stop framework 2>/dev/null
initctl stop webreader 2>/dev/null
nohup /bin/sh $D/dash.sh >/dev/null 2>&1 &
echo "看板主循环已启动（日志：/mnt/us/dashboard/dash.log）"
exit 0
"""

STOP_SH = """#!/bin/sh
# 停止看板主循环：写 stop 标志，下次唤醒后 dash.sh 退出并恢复原生界面
touch /mnt/us/dashboard/stop
echo "已设置停止标志；下次唤醒后看板主循环将退出并恢复原生界面"
exit 0
"""

FILES = {
    "config.xml": CONFIG_XML,
    "menu.json": MENU_JSON,
    "diag.sh": DIAG_SH,
    "show.sh": SHOW_SH,
    "part.sh": PART_SH,
    "suspend.sh": SUSPEND_SH,
    "bright.sh": BRIGHT_SH,
    "clear.sh": CLEAR_SH,
    "quit.sh": QUIT_SH,
    "dash.sh": DASH_SH,
    "start.sh": START_SH,
    "stop.sh": STOP_SH,
}


def looks_like_kindle(root):
    return os.path.isdir(os.path.join(root, "documents")) and os.path.isdir(
        os.path.join(root, "system")
    )


def deploy(drive, dry_run=False):
    drive = drive.rstrip("\\/") + "\\"
    if not looks_like_kindle(drive):
        print(f"[错误] {drive} 看起来不是 Kindle 根目录（缺少 documents 或 system）")
        return 1

    ext_dir = os.path.join(drive, "extensions", EXT_NAME)
    print(f"目标：{ext_dir}")
    print()

    if not dry_run:
        os.makedirs(ext_dir, exist_ok=True)

    for name, content in FILES.items():
        target = os.path.join(ext_dir, name)
        if dry_run:
            print(f"  [将写入] {name}  ({len(content)} 字节)")
            continue
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        print(f"  [已写入] {name}  ({len(content)} 字节)")

    # 测试画面
    full = make_full_frame()
    strip = make_strip()
    images = [
        ("t_gray8.png", TARGET_W, TARGET_H, full, 0),
        ("strip_gray8.png", TARGET_W, STRIP_H, strip, 0),
    ]
    for name, w, h, px, ct in images:
        target = os.path.join(ext_dir, name)
        if dry_run:
            print(f"  [将写入] {name}  ({w}x{h} colortype={ct})")
            continue
        size = write_png(target, w, h, px, ct)
        print(f"  [已写入] {name}  ({w}x{h} colortype={ct}, {size} 字节)")

    # 默认前光亮度配置（已存在则不覆盖，保留用户调节过的值）
    cfg = os.path.join(drive, "dashboard", "fl_intensity")
    if not dry_run and not os.path.exists(cfg):
        os.makedirs(os.path.dirname(cfg), exist_ok=True)
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(str(FL_DEFAULT))
        print(f"  [已写入] dashboard/fl_intensity  (默认 {FL_DEFAULT})")

    # server.conf：填好 Windows 端 token，HOST 留占位让用户改成电脑内网 IP
    tok_path = os.path.expanduser("~/.dash_server/token")
    tok = ""
    if os.path.isfile(tok_path):
        with open(tok_path, "r", encoding="utf-8") as f:
            tok = f.read().strip()
    server_conf = (
        "# Kindle 看板 server 配置（由 kindle_ext_builder.py 生成，TOKEN 已自动填入）\n"
        "# 只需把 HOST 改成电脑在同一 wifi 的内网 IP（路由器给电脑设固定 IP 最佳）\n"
        "HOST=192.168.1.100\n"
        "PORT=8765\n"
        f"TOKEN={tok}\n"
    )
    scfg = os.path.join(drive, "dashboard", "server.conf")
    if dry_run:
        print("  [将写入] dashboard/server.conf  (TOKEN 已填，HOST 占位)")
    elif os.path.exists(scfg):
        # 已存在：保留用户改过的 HOST，只确保 TOKEN 不为空（否则补填）
        print(f"  [保留] dashboard/server.conf  ({scfg} 已存在，不覆盖)")
        try:
            with open(scfg, "r", encoding="utf-8") as f:
                lines = f.readlines()
            need = False
            new_lines = []
            for ln in lines:
                if ln.startswith("TOKEN=") and tok and len(ln.strip()) <= len("TOKEN="):
                    new_lines.append(f"TOKEN={tok}\n")
                    need = True
                else:
                    new_lines.append(ln)
            if need:
                with open(scfg, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                print("         （已补填缺失的 TOKEN）")
        except Exception as e:
            print(f"  [警告] 读取 server.conf 失败，未改动：{e}")
    else:
        os.makedirs(os.path.dirname(scfg), exist_ok=True)
        with open(scfg, "w", encoding="utf-8", newline="") as f:
            f.write(server_conf)
        print(f"  [已写入] dashboard/server.conf  (TOKEN={'已填' if tok else '缺失！需手填'})")

    print()
    print("部署完成。下一步：")
    print("  1. 安全弹出 Kindle，拔掉数据线")
    print("  2. 书库里打开 KUAL → KDashboard")
    print("  3. 亮度嫌暗：点「5. 亮度 +3」实时调亮（默认已拉到满级 24）")
    print("  4. 想跑通真机：先确认电脑端 dash_server 已起、记下电脑内网 IP")
    print("  5. 编辑 Kindle 上 dashboard/server.conf，把 HOST 改成电脑内网 IP")
    print("  6. 点「8. 启动看板主循环」—— 后台取图+画屏+自动省电挂起")
    print("  7. 看板跑起来后，点「9. 停止看板」可在下次唤醒后退出")
    print("  8. 测试项：1 上屏矩阵 / 2 局部定位 / 3 诊断 / 7 挂起唤醒 仍可单独跑")
    return 0


def main():
    ap = argparse.ArgumentParser(description="生成并部署 Kindle KUAL 扩展 kdashboard")
    ap.add_argument("--drive", required=True, help="Kindle 盘符，例如 F:")
    ap.add_argument("--dry", action="store_true", help="只打印计划，不实际写入")
    args = ap.parse_args()
    return deploy(args.drive, args.dry)


if __name__ == "__main__":
    sys.exit(main())
