# -*- coding: utf-8 -*-
"""
Kindle 工作控制台 · HUD 风格渲染器

目标设备：Kindle Paperwhite 3，竖屏 1072 x 1448，16 级灰度。

设计参考：机甲/HUD 模块化仪表盘
- 粗黑边框 + 模块编号标签
- 顶部：位置 / 主显示 / 系统状态
- 24 小时天气扫描条
- 日历 / 笔记 / 待办 / Token 用量
- 底部快捷图标栏

输出：8bit 灰度 PNG（colortype 0），渲染阶段主动量化到 16 级。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1072
HEIGHT = 1448
GRAY_LEVELS = 16

# 16 级灰阶调色板
BLACK = 0
INK = 32
GRAY_DARK = 96
GRAY_MID = 140
GRAY_LIGHT = 200
GRAY_FAINT = 235
WHITE = 255

# 天气图标精灵图目录（由 tools/build_weather_icons.py 预生成）
ASSET_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets"
)
WEATHER_ICON_DIR = os.path.join(ASSET_DIR, "weather_icons")
# 旧友好的天气名 → QWeather 天气代码（和风天气 API 直接返回数字代码）
NAME_TO_QWEATHER = {
    "sunny": "100", "clear": "100", "fine": "100",
    "partly": "103", "few": "102",
    "cloudy": "101", "overcast": "104",
}

MARGIN = 32
GAP = 16
PANEL_BORDER = 4
TITLE_H = 34

FONT_CANDIDATES = [
    ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc"),   # 微软雅黑
    ("C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simhei.ttf"), # 黑体
]


# ------------------------------------------------------------------ 数据模型
@dataclass
class Weather:
    location: str = "上海市黄浦区"
    temp_now: Optional[int] = None
    temp_high: Optional[int] = None
    temp_low: Optional[int] = None
    text: str = "—"
    icon_code: Optional[str] = None
    observed_at: Optional[datetime] = None
    stale: bool = False


@dataclass
class HourlyWeather:
    hour: str = "12:00"
    temp: int = 25
    icon_code: str = "sunny"       # sunny / cloudy / partly / overcast


@dataclass
class SysStatusItem:
    label: str = ""
    value: str = ""
    ratio: float = 0.0             # 0.0 - 1.0，用于右侧小条


@dataclass
class Quota:
    label: str = ""
    remain_ratio: float = 0.0
    reset_text: str = ""
    stale: bool = False


@dataclass
class CodexTask:
    title: str = ""
    state: str = "idle"
    state_text: str = "空闲"
    elapsed: str = ""
    last_progress: str = ""


@dataclass
class Intervention:
    level: str = ""
    title: str = ""
    detail: str = ""


@dataclass
class Todo:
    title: str
    due_text: str = ""
    priority: str = ""
    overdue: bool = False


@dataclass
class TokenUsage:
    name: str = ""
    usage_ratio: float = 0.0       # 已用比例
    status: str = ""             # 右侧主状态文字，如 剩96% / 需终端
    detail: str = ""             # 与 status 合并到顶行的短补充，如 5h窗口
    note: str = ""              # 进度条下方小字补充，如 累计88.0M（不与顶行冲突）
    weekly_ratio: float = 0.0    # 周窗口已用比例（Codex secondary），同一行内子条
    weekly_remaining: int = 0    # 周窗口剩余百分比，如 2
    weekly_reset: str = ""       # 周窗口重置文字，如 约5d后重置
    weekly_status: str = ""      # 周窗口状态文字（覆盖 weekly_remaining）。如「额度未知」
                                 # 表示 Codex 主额度拿不到、但仍要画一条「额度未知·周窗口」子条


@dataclass
class BottomIcon:
    label: str = ""
    icon_type: str = "circle"      # command / gear / auto / wifi / number / board


@dataclass
class DeviceStatus:
    pc_online: bool = True
    kindle_battery: Optional[int] = None
    kindle_charging: bool = False
    updated_at: Optional[datetime] = None


@dataclass
class Snapshot:
    now: datetime = field(default_factory=datetime.now)
    last_updated: Optional[datetime] = None   # 数据最后刷新时间，供 SYS STATUS 显示
    weather: Weather = field(default_factory=Weather)
    hourly_weather: List[HourlyWeather] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    make_summary: List[str] = field(default_factory=list)  # 今日日记「搞点什么」区块概要
    sys_status: List[SysStatusItem] = field(default_factory=list)
    tokens: List[TokenUsage] = field(default_factory=list)
    todos: List[Todo] = field(default_factory=list)
    bottom_icons: List[BottomIcon] = field(default_factory=list)
    # 旧字段保留，避免外部引用报错
    intervention: Optional[Intervention] = None
    codex: CodexTask = field(default_factory=CodexTask)
    quotas: List[Quota] = field(default_factory=list)
    device: DeviceStatus = field(default_factory=DeviceStatus)
    page: int = 1
    page_count: int = 3


# ------------------------------------------------------------------ 渲染器
class DashboardRenderer:
    def __init__(self, width: int = WIDTH, height: int = HEIGHT,
                 gray_levels: int = GRAY_LEVELS):
        self.w = width
        self.h = height
        self.gray_levels = gray_levels
        self._fonts()

        # 16 级量化查找表
        step = 255.0 / (gray_levels - 1)
        self._lut = [int(round(round(v / step) * step)) for v in range(256)]

        # 天气图标精灵图缓存
        self._sprite_cache: dict = {}

    # ---- 字体 ----
    def _fonts(self):
        bold_path = regular_path = None
        for b, r in FONT_CANDIDATES:
            if os.path.exists(b) and os.path.exists(r):
                bold_path, regular_path = b, r
                break
        if bold_path is None:
            raise RuntimeError("未找到中文字体（需要 msyh.ttc 或 simhei.ttf）")

        def mk(size):
            return ImageFont.truetype(regular_path, size)

        def mkb(size):
            return ImageFont.truetype(bold_path, size)

        self.f_title = mkb(20)           # 模块标题
        self.f_code = mkb(18)            # 编号标签
        self.f_time = mkb(86)            # 主时间
        self.f_date = mk(26)             # 日期
        self.f_temp_big = mkb(56)        # 当前温度
        self.f_clock = mkb(48)           # 最近更新时间大数字
        self.f_temp = mkb(26)            # 小时温度
        self.f_weather = mk(24)          # 天气文字
        self.f_mode = mkb(48)            # AUTO 等模式字
        self.f_status = mk(22)           # 状态小字
        self.f_body = mk(26)             # 正文
        self.f_body_b = mkb(26)          # 正文粗
        self.f_small = mk(22)            # 小字
        self.f_tiny = mk(20)             # 极小字
        self.f_icon = mk(20)             # 底部图标文字

    # ---- 工具 ----
    def _fit(self, draw, text, font, max_w):
        if not text:
            return ""
        if draw.textlength(text, font=font) <= max_w:
            return text
        ell = "…"
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if draw.textlength(text[:mid] + ell, font=font) <= max_w:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + ell

    def _wrap_text(self, draw, text, font, max_w, max_lines=6):
        """按像素宽度做贪心换行：
        - 中文逐字可断；
        - 英文/数字等连续非 CJK 序列按词边界保持完整；
        - 超过 max_lines 时，最后一行用 _fit 截断并加省略号。
        """
        if not text:
            return []

        # 拆分为最小排版单元：单个 CJK 字符（含中文标点），或一段连续非 CJK 文本
        tokens = re.findall(
            r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]|[^\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+",
            text)

        lines = []
        cur = ""
        for idx, tok in enumerate(tokens):
            if tok == "\n":
                lines.append(cur)
                cur = ""
                if len(lines) >= max_lines:
                    break
                continue

            trial = cur + tok
            if draw.textlength(trial, font=font) <= max_w:
                cur = trial
                continue

            # tok 单放不进空行：必须拆（通常是超长 URL/无空格英文）
            if not cur and draw.textlength(tok, font=font) > max_w:
                # 对该 token 逐字强断
                for ch in tok:
                    if draw.textlength(cur + ch, font=font) <= max_w:
                        cur += ch
                    else:
                        if cur:
                            lines.append(cur)
                        cur = ch
                        if len(lines) >= max_lines:
                            break
                continue

            # 正常换行
            if cur:
                lines.append(cur)
            cur = tok
            if len(lines) >= max_lines - 1:
                # 已到最后一行允许空间，剩余内容截断加省略号
                remaining = "".join(tokens[idx:])
                cur = self._fit(draw, remaining, font, max_w)
                break

        if cur:
            lines.append(cur)
        return lines[:max_lines]

    def _draw_bulleted_items(self, draw, entries, x0, y0, x1, y1, cy,
                             *, font, max_lines=4, line_h=32, indent=20,
                             dot_r=3, gap=4):
        """绘制带项目符号的多条文本：每条自动换行（CJK 友好），符号置于首行左。
        返回新的 cy（已累加留白）。超出面板底边 y1 时停止绘制以免溢出。"""
        for entry in entries:
            if cy > y1 - 24:
                break
            wrapped = self._wrap_text(draw, entry, font,
                                      (x1 - x0) - indent, max_lines=max_lines)
            if not wrapped:
                continue
            # 项目符号：小实心圆，与第一行文字垂直居中
            dot_cx = x0 + dot_r + 2
            dot_cy = cy + 13
            draw.ellipse((dot_cx - dot_r, dot_cy - dot_r,
                          dot_cx + dot_r, dot_cy + dot_r), fill=INK)
            for ln in wrapped:
                if cy > y1 - 24:
                    break
                draw.text((x0 + indent, cy), ln, font=font, fill=INK)
                cy += line_h
            cy += gap   # 条目间留白
        return cy

    def _quantize(self, img: Image.Image) -> Image.Image:
        return img.point(self._lut)

    def _panel_frame(self, draw, box, title, code="", fill=WHITE,
                     border=BLACK, title_fill=GRAY_FAINT):
        """画带标题条的模块外框。"""
        x0, y0, x1, y1 = box
        # 主框
        draw.rectangle(box, outline=border, width=PANEL_BORDER, fill=fill)
        # 标题条
        title_y1 = y0 + TITLE_H
        draw.rectangle((x0 + PANEL_BORDER, y0 + PANEL_BORDER,
                        x1 - PANEL_BORDER, title_y1),
                       fill=title_fill)
        draw.line((x0 + PANEL_BORDER, title_y1,
                   x1 - PANEL_BORDER, title_y1),
                  fill=border, width=2)

        # 编号标签（左侧斜切风格块）
        tag_w = 72 if code else 0
        if code:
            # 画小三角装饰
            draw.polygon([(x0 + PANEL_BORDER, y0 + PANEL_BORDER),
                          (x0 + PANEL_BORDER + 10, y0 + PANEL_BORDER),
                          (x0 + PANEL_BORDER, y0 + PANEL_BORDER + 10)],
                         fill=GRAY_DARK)
            draw.text((x0 + PANEL_BORDER + 12, (y0 + title_y1) // 2),
                      code, font=self.f_code, fill=INK, anchor="lm")

        # 标题
        title_x = x0 + PANEL_BORDER + (tag_w if code else 14)
        draw.text((title_x, (y0 + title_y1) // 2), title,
                  font=self.f_title, fill=BLACK, anchor="lm")

        # 右侧装饰斜线
        for dx in (12, 20):
            draw.line((x1 - PANEL_BORDER - dx, y0 + PANEL_BORDER + 6,
                       x1 - PANEL_BORDER - dx + 8, y0 + PANEL_BORDER + 6),
                      fill=GRAY_DARK, width=2)

        # 返回内容区域
        return (x0 + PANEL_BORDER + 10, title_y1 + 8,
                x1 - PANEL_BORDER - 10, y1 - PANEL_BORDER - 8)

    def _progress_bar(self, draw, box, ratio, fill=BLACK, back=GRAY_LIGHT,
                      border=BLACK, height=22):
        """粗边框进度条。"""
        x0, y0, x1, y1 = box
        # 槽
        draw.rectangle((x0, y0, x1, y0 + height), outline=border,
                       width=2, fill=back)
        ratio = max(0.0, min(1.0, ratio))
        inner_w = x1 - x0 - 4
        if ratio > 0 and inner_w > 0:
            fw = max(4, int(inner_w * ratio))
            draw.rectangle((x0 + 2, y0 + 2, x0 + 2 + fw, y0 + height - 2),
                           fill=fill)

    def _segmented_bar(self, draw, box, ratio, n=20, gap=3, cell_h=20,
                       used=BLACK, unused=GRAY_LIGHT, border=BLACK):
        """格子组成的长条：已用部分黑格，未用部分灰格。"""
        x0, y0, x1, _y1 = box
        ratio = max(0.0, min(1.0, ratio))
        inner_w = x1 - x0
        if inner_w <= 0 or n <= 0:
            return
        cell_w = (inner_w - gap * (n - 1)) / n
        if cell_w <= 0:
            return
        used_n = int(round(ratio * n))
        for i in range(n):
            cx = x0 + i * (cell_w + gap)
            ix0 = int(round(cx))
            ix1 = int(round(cx + cell_w))
            fill = used if i < used_n else unused
            draw.rectangle((ix0, y0, ix1, y0 + cell_h), fill=fill,
                           outline=border, width=1)

    def _checkbox(self, draw, x, y, size=22, checked=False):
        """方框复选框。"""
        draw.rectangle((x, y, x + size, y + size), outline=BLACK, width=2,
                       fill=WHITE)
        if checked:
            # 画对勾
            draw.line([(x + 4, y + size // 2), (x + size // 3, y + size - 4),
                       (x + size - 3, y + 4)], fill=BLACK, width=3)

    # ---- 天气图标（QWeather 精灵图） ----
    def _weather_sprite(self, code):
        """读取预生成的 QWeather 图标精灵图（黑色实心、透明底）。"""
        if code in self._sprite_cache:
            return self._sprite_cache[code]
        sprite = None
        path = os.path.join(WEATHER_ICON_DIR, f"{code}.png")
        if os.path.exists(path):
            sprite = Image.open(path).convert("RGBA")
        self._sprite_cache[code] = sprite
        return sprite

    def _draw_weather_icon(self, img, cx, cy, size, code):
        """把 QWeather 精灵图按 size（最长边）等比贴到 img 上，黑色实心、透明底。"""
        code = NAME_TO_QWEATHER.get(code, code)
        sprite = self._weather_sprite(code)
        if sprite is None:
            # 缺图标时画占位圆，避免空白
            d = ImageDraw.Draw(img)
            r = size // 2
            d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=BLACK, width=2)
            return
        w, h = sprite.size
        scale = min(size / w, size / h)
        nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        sp = sprite.resize((nw, nh), Image.LANCZOS)
        x = int(round(cx - nw / 2))
        y = int(round(cy - nh / 2))
        mask = sp.split()[-1]
        img.paste(0, (x, y, x + nw, y + nh), mask=mask)

    def _bottom_icon(self, draw, cx, cy, size, icon_type):
        """底部圆形图标。"""
        r = size // 2
        # 外框
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=BLACK,
                     width=3, fill=GRAY_FAINT)
        # 内部符号
        if icon_type == "command":
            # 三角播放
            draw.polygon([(cx - 5, cy - 7), (cx + 7, cy), (cx - 5, cy + 7)],
                         fill=BLACK)
        elif icon_type == "gear":
            draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), outline=BLACK, width=2)
            for angle in range(0, 360, 45):
                import math
                a = math.radians(angle)
                x1 = cx + int(6 * math.cos(a))
                y1 = cy + int(6 * math.sin(a))
                x2 = cx + int(9 * math.cos(a))
                y2 = cy + int(9 * math.sin(a))
                draw.line((x1, y1, x2, y2), fill=BLACK, width=2)
        elif icon_type == "auto":
            draw.text((cx, cy), "A", font=self.f_body_b, fill=BLACK, anchor="mm")
        elif icon_type == "wifi":
            # 弧线
            for i, arc_r in enumerate((4, 7, 10)):
                draw.arc((cx - arc_r, cy - arc_r, cx + arc_r, cy + arc_r),
                         start=-45, end=45, fill=BLACK, width=2)
        elif icon_type == "number":
            draw.text((cx, cy), "256", font=self.f_tiny, fill=BLACK, anchor="mm")
        elif icon_type == "board":
            draw.rectangle((cx - 6, cy - 5, cx + 6, cy + 5), outline=BLACK, width=2)
            draw.line((cx - 6, cy + 1, cx + 6, cy + 1), fill=BLACK, width=1)
        else:
            draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill=BLACK)

    # ---- 各区块 ----
    def _panel_location(self, draw, box, weather, img):
        cx = self._panel_frame(draw, box, "LOCATION", "L-01")
        x0, y0, x1, y1 = cx
        cxp = (x0 + x1) // 2  # 水平中心

        # 位置（居中）
        loc = self._fit(draw, weather.location, self.f_body_b, x1 - x0)
        lw = draw.textlength(loc, font=self.f_body_b)
        draw.text((cxp - lw / 2, y0 + 14), loc, font=self.f_body_b, fill=BLACK)

        # 天气图标 + 温度（居中一排，整体稍下移让布局更居中）
        t = "—" if weather.temp_now is None else f"{weather.temp_now}°"
        tw = draw.textlength(t, font=self.f_temp_big)
        icon_size = 46
        gap = 14
        total = icon_size + gap + tw
        start_x = cxp - total // 2
        self._draw_weather_icon(img, start_x + icon_size // 2, y0 + 100,
                               icon_size, weather.icon_code or "100")
        draw.text((start_x + icon_size + gap, y0 + 100), t,
                  font=self.f_temp_big, fill=BLACK, anchor="lm")

        # 天气文字（居中，上移靠近图标/温度行）
        sub = weather.text
        if weather.temp_high is not None and weather.temp_low is not None:
            sub = f"{weather.text} {weather.temp_low}°/{weather.temp_high}°"
        sub = self._fit(draw, sub, self.f_weather, x1 - x0)
        sw = draw.textlength(sub, font=self.f_weather)
        draw.text((cxp - sw / 2, y0 + 150), sub, font=self.f_weather,
                  fill=GRAY_DARK)

    def _panel_main_display(self, draw, box, snap):
        cx = self._panel_frame(draw, box, "MAIN DISPLAY", "M-01")
        x0, y0, x1, y1 = cx

        # 标签行：最近更新（取代原实时钟）
        draw.text((x0, y0 + 8), "最近更新", font=self.f_body_b, fill=GRAY_DARK)
        lb_w = draw.textlength("最近更新", font=self.f_body_b)
        draw.text((x0 + lb_w + 12, y0 + 12), "LAST SYNC",
                  font=self.f_tiny, fill=GRAY_LIGHT)

        # 大号时间：数据最后刷新时刻（非实时钟）
        lu = snap.last_updated or snap.now
        ts = lu.strftime("%H:%M:%S")
        draw.text((x0, y0 + 86), ts, font=self.f_clock, fill=BLACK, anchor="lm")

        # 分隔线（上移，给下方文字留余量）
        sep_y = y0 + 124
        draw.line((x0, sep_y, x1, sep_y), fill=GRAY_DARK, width=2)

        # 下方：日期 + 数据源
        cy = sep_y + 14
        week = "一二三四五六日"[lu.weekday()]
        date_str = f"{lu.year}-{lu.month:02d}-{lu.day:02d} 周{week}"
        draw.text((x0, cy), date_str, font=self.f_small, fill=GRAY_DARK)
        cy += 30
        src = "数据源: PC · 在线" if snap.device.pc_online else "数据源: 缓存/离线"
        draw.text((x0, cy), src, font=self.f_tiny, fill=GRAY_LIGHT)

    def _panel_sys_status(self, draw, box, snap):
        cx = self._panel_frame(draw, box, "SYS STATUS", "S-01")
        x0, y0, x1, y1 = cx

        # 标签行：KINDLE 电量
        draw.text((x0, y0 + 8), "KINDLE 电量", font=self.f_body_b, fill=GRAY_DARK)
        lb_w = draw.textlength("KINDLE 电量", font=self.f_body_b)
        draw.text((x0 + lb_w + 12, y0 + 12), "BATTERY",
                  font=self.f_tiny, fill=GRAY_LIGHT)

        # 大号百分比
        batt = snap.device.kindle_battery
        charging = snap.device.kindle_charging
        if batt is None:
            big = "—"
            ratio = 0.0
        else:
            big = f"{int(batt)}%"
            ratio = max(0.0, min(1.0, batt / 100.0))
        draw.text((x0, y0 + 86), big, font=self.f_clock, fill=BLACK, anchor="lm")

        # 分隔线 + 电量条（横线整体上一移，给状态行留余量）
        sep_y = y0 + 124
        draw.line((x0, sep_y, x1, sep_y), fill=GRAY_DARK, width=2)
        bar_y = sep_y + 16
        self._segmented_bar(draw, (x0, bar_y, x1, bar_y), ratio, n=20, cell_h=20)

        # 状态行：充电状态 + 最近上报时刻
        cy = bar_y + 30
        if batt is None:
            status = "看板端待上报电量"
        else:
            status = "充电中" if charging else "未充电"
            if snap.device.updated_at:
                status += f" · 上报 {snap.device.updated_at:%H:%M:%S}"
            else:
                status += " · 已上报"
        draw.text((x0, cy), status, font=self.f_small, fill=GRAY_DARK)

    def _panel_hourly_weather(self, draw, box, hourly, img):
        cx = self._panel_frame(draw, box, "24H WEATHER SCAN", "W-01",
                               title_fill=GRAY_FAINT)
        x0, y0, x1, y1 = cx

        # 右侧 NEXT 6H，放在标题条内右侧；向左避让装饰斜线，避免 H 被遮挡
        draw.text((box[2] - PANEL_BORDER - 36, box[1] + TITLE_H // 2),
                  "NEXT 6H", font=self.f_small, fill=GRAY_DARK,
                  anchor="rm")

        if not hourly:
            return

        n = len(hourly)
        item_w = (x1 - x0) // n
        icon_size = 40
        for i, h in enumerate(hourly):
            ix = x0 + i * item_w + item_w // 2
            # 时间/图标/温度整体下移，在面板内稍微居中
            draw.text((ix, y0 + 16), h.hour, font=self.f_small, fill=GRAY_DARK,
                      anchor="mt")
            # 图标（中部居中）
            self._draw_weather_icon(img, ix, y0 + 66, icon_size, h.icon_code)
            # 温度（底部居中）
            draw.text((ix, y0 + 108), f"{h.temp}°", font=self.f_temp,
                      fill=BLACK, anchor="mm")

    def _panel_calendar(self, draw, box, snap):
        # 内容为空时直接不画整个面板（不画「搞点什么」标题框），让位置留白
        # 给同行的 NOTES 或下方 TOKENS，让看板更紧凑。
        if not snap.make_summary:
            return
        cx = self._panel_frame(draw, box, "搞点什么", "C-02")
        x0, y0, x1, y1 = cx

        # 内容来自日记「🛠️ 搞点什么」区块的『概要』：摘要文字，非待办清单
        lines = snap.make_summary
        cy = y0 + 10
        self._draw_bulleted_items(draw, lines[:3], x0, y0, x1, y1, cy,
                                  font=self.f_body, max_lines=4, line_h=32,
                                  indent=20, dot_r=3, gap=6)

    def _panel_notes(self, draw, box, notes):
        # 空内容时不画 NOTES 面板，让位置让给其他面板
        if not notes:
            return
        cx = self._panel_frame(draw, box, "NOTES", "N-02")
        x0, y0, x1, y1 = cx
        cy = y0 + 8
        # 每条自动换行（不截断），首行带项目符号
        self._draw_bulleted_items(draw, notes[:3], x0, y0, x1, y1, cy,
                                  font=self.f_body, max_lines=4, line_h=30,
                                  indent=20, dot_r=3, gap=4)

    def _panel_todo(self, draw, box, todos):
        # 空内容时不画 TO DO QUEUE 面板，让位置让给其他面板
        if not todos:
            return
        cx = self._panel_frame(draw, box, "TO DO QUEUE", "O-04")
        x0, y0, x1, y1 = cx
        cy = y0 + 14
        box_size = 22
        text_x = x0 + 32
        line_h = 32
        for t in todos[:4]:
            if cy > y1 - 30:
                break
            first_cy = cy

            # 右侧标签（优先级/截止）的实际宽度
            right = " ".join(x for x in (t.priority, t.due_text) if x)
            label_w = int(draw.textlength(right, font=self.f_small)) if right else 0

            # 文字可用宽度：没标签时尽量用满整栏；有标签时只避让标签宽度 + 间距
            wrap_w = (x1 - text_x) - (label_w + 12 if right else 0)

            # 自动换行，避免长标题超出边框
            wrapped = self._wrap_text(draw, t.title, self.f_body,
                                      wrap_w, max_lines=3)
            if not wrapped:
                wrapped = [""]

            # 方框与首行文字垂直居中：行高 32，方框 22 → 下移 5px
            box_y = first_cy + (line_h - box_size) // 2
            self._checkbox(draw, x0, box_y, size=box_size,
                           checked=t.overdue)

            for ln in wrapped:
                if cy > y1 - 24:
                    break
                draw.text((text_x, cy), ln, font=self.f_body, fill=BLACK)
                cy += line_h

            # 右侧标签固定在首行右侧，与方框垂直居中
            if right:
                label_y = box_y + box_size // 2
                draw.text((x1, label_y), right, font=self.f_small,
                          fill=GRAY_DARK, anchor="ra")
            cy += 4

    def _panel_tokens(self, draw, box, tokens):
        cx = self._panel_frame(draw, box, "用量 USAGE", "R-02")
        x0, y0, x1, y1 = cx
        cy = y0 + 16
        for tk in tokens[:4]:
            if cy > y1 - 80:
                break
            # 名称
            draw.text((x0, cy), tk.name, font=self.f_body_b, fill=BLACK)
            # 右侧主状态：status 与 detail 合并到同一行（如「剩96%·5h窗口」）
            right_text = (f"{tk.status}·{tk.detail}"
                          if (tk.status and tk.detail) else (tk.status or tk.detail or "—"))
            draw.text((x1, cy - 2), right_text, font=self.f_body,
                      fill=BLACK, anchor="ra")

            # 主条（5h / 主窗口）：与上方文字拉开间距，避免字贴条
            bar_y = cy + 36
            self._segmented_bar(draw, (x0, bar_y, x1, bar_y), tk.usage_ratio)

            # 进度条下方小字补充（如 累计88.0M · 约X后重置）
            sub = bar_y + 30
            if tk.note:
                draw.text((x1, sub), tk.note, font=self.f_small,
                          fill=GRAY_DARK, anchor="ra")
                sub += 30

            # 周窗口子行：同一 Codex 行内，不另起名称；右侧状态优先取
            # weekly_status（如「额度未知」），缺省再拼 weekly_remaining。
            # 触发条件：weekly_ratio>0（正常数据）或 weekly_status 非空（兜底显示）。
            if tk.weekly_ratio or tk.weekly_status:
                if tk.weekly_status:
                    wk_status = tk.weekly_status
                else:
                    wk_status = (f"剩{tk.weekly_remaining}%" if tk.weekly_remaining else "—")
                wk_status += "·周窗口"
                draw.text((x1, sub), wk_status, font=self.f_body,
                          fill=BLACK, anchor="ra")
                # 兜底场景（额度未知）下进度条占位 0；正常场景按真实比例
                bar_ratio = tk.weekly_ratio if tk.weekly_ratio else 0.0
                wbar_y = sub + 36
                self._segmented_bar(draw, (x0, wbar_y, x1, wbar_y), bar_ratio)
                sub = wbar_y + 30
                if tk.weekly_reset:
                    draw.text((x1, sub), tk.weekly_reset, font=self.f_small,
                              fill=GRAY_DARK, anchor="ra")
                    sub += 30

            cy = sub + 26

    def _bottom_bar(self, draw, box, icons):
        x0, y0, x1, y1 = box
        # 顶部分隔线
        draw.line((x0, y0, x1, y0), fill=BLACK, width=3)

        if not icons:
            return

        n = len(icons)
        icon_w = (x1 - x0) // n
        for i, ic in enumerate(icons):
            cx = x0 + i * icon_w + icon_w // 2
            cy = y0 + 10 + (y1 - y0 - 20) // 2
            self._bottom_icon(draw, cx, cy - 10, 44, ic.icon_type)
            draw.text((cx, y1 - 18), ic.label, font=self.f_icon, fill=BLACK,
                      anchor="mm")

    # ---- 主渲染 ----
    def render(self, snap: Snapshot) -> Image.Image:
        img = Image.new("L", (self.w, self.h), WHITE)
        draw = ImageDraw.Draw(img)

        x0 = MARGIN
        x1 = self.w - MARGIN
        y = MARGIN

        # 顶部三模块
        top_h = 260
        col_w = (x1 - x0 - GAP * 2) // 3
        self._panel_location(draw, (x0, y, x0 + col_w, y + top_h),
                             snap.weather, img)
        self._panel_main_display(draw,
                                 (x0 + col_w + GAP, y,
                                  x0 + 2 * col_w + GAP, y + top_h),
                                 snap)
        self._panel_sys_status(draw,
                               (x0 + 2 * (col_w + GAP), y, x1, y + top_h),
                               snap)
        y += top_h + GAP

        # 24H 天气
        weather_h = 190
        self._panel_hourly_weather(draw, (x0, y, x1, y + weather_h),
                                    snap.hourly_weather, img)
        y += weather_h + GAP

        # 第三行：日历 + 笔记
        row3_h = 350
        mid = (x0 + x1 - GAP) // 2
        self._panel_calendar(draw, (x0, y, mid, y + row3_h), snap)
        self._panel_notes(draw, (mid + GAP, y, x1, y + row3_h), snap.notes)
        y += row3_h + GAP

        # 底部图标栏（固定高度、贴底），先确定其顶边
        bottom_h = 110
        bottom_top = self.h - MARGIN - bottom_h

        # 第四行：Todo + Token
        # 高度由「剩余垂直空间」推导：bottom_top - GAP - 当前 y，
        # 保证整张看板始终落在 1448 画布内、且不与底部图标栏重叠。
        row4_h = max(220, bottom_top - GAP - y)
        self._panel_todo(draw, (x0, y, mid, y + row4_h), snap.todos)
        self._panel_tokens(draw, (mid + GAP, y, x1, y + row4_h), snap.tokens)

        # 底部图标栏
        self._bottom_bar(draw, (x0, bottom_top, x1, self.h - MARGIN),
                         snap.bottom_icons)

        return self._quantize(img)

    def save(self, snap: Snapshot, path: str) -> str:
        img = self.render(snap)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        img.save(path, "PNG", optimize=True)
        return path


# ------------------------------------------------------------------ 演示数据

def demo_snapshot() -> Snapshot:
    """按示例图构造假数据，不读取任何真实账户信息。"""
    return Snapshot(
        now=datetime(2026, 7, 30, 8, 59),
        last_updated=datetime(2026, 7, 30, 8, 58, 17),
        weather=Weather(
            location="上海 · 嘉定",
            temp_now=34,
            temp_high=37,
            temp_low=28,
            text="晴",
            icon_code="100",
        ),
        hourly_weather=[
            HourlyWeather("09:00", 34, "100"),
            HourlyWeather("10:00", 35, "103"),
            HourlyWeather("11:00", 36, "103"),
            HourlyWeather("12:00", 37, "101"),
            HourlyWeather("13:00", 37, "101"),
            HourlyWeather("14:00", 37, "104"),
        ],
        sys_status=[
            SysStatusItem("MODE", "AUTO", 0.0),
            SysStatusItem("AVAILABLE", "", 0.65),
        ],
        notes=[
            "虽然名义叫第二块屏幕，但 Kindle 只负责安静地显示最后一个有效版本，不承载任何交互。",
            "MOC 负责思考与排期，把结构化结论推送到设备；Kindle 是单向的只读输出端。",
            "后续若要做双向同步，需要先在 MOC 侧补齐回写接口与冲突解决策略。",
        ],
        make_summary=[
            "把 Kindle Dashboard 的第一版真正跑到 Kindle 上：先设计界面、再梳理数据来源与刷新方案，最后刷机上机验证。",
            "目标不是一次把方案做到完美，而是先完整走通这类硬件项目从设计到部署的路径。",
        ],
        todos=[
            Todo("确认三种主题（浅色/深色/自动）在 16 级灰度下的实际可读性与对比度", "", "", overdue=True),
            Todo("购买支持数据传输的 USB 线", "", "", overdue=False),
            Todo("准备天气数据源并接入预报刷新", "", "", overdue=False),
            Todo("完成 Kindle 只读备份与恢复验证", "", "", overdue=False),
        ],
        tokens=[
            TokenUsage("CODEX", 0.36, "剩64%", "5h窗口", "累计88.0M · 约3h后重置",
                       weekly_ratio=0.62, weekly_remaining=38,
                       weekly_reset="约5d后重置"),
            TokenUsage("CLAUDE", 0.0, "UNAVAILABLE"),
        ],
        bottom_icons=[
            BottomIcon("EVA COMMAND", "command"),
            BottomIcon("GRAY", "gear"),
            BottomIcon("AUTO", "auto"),
            BottomIcon("WIFI", "wifi"),
            BottomIcon("256", "number"),
            BottomIcon("ONBOARD", "board"),
        ],
    )


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "preview_hud.png"
    r = DashboardRenderer()
    r.save(demo_snapshot(), out)
    print(f"已生成 {out}  ({WIDTH}x{HEIGHT}, {GRAY_LEVELS} 级灰度)")
