# -*- coding: utf-8 -*-
"""
构建真实天气看板：拉取 QWeather 实时 + 24h 数据，渲染成 1072x1448 PNG。

用法：
    python src/build_dashboard.py                 # 渲染到 samples/preview_home.png
    python src/build_dashboard.py out.png         # 指定输出路径

其他面板（待办 / Token / 笔记）从 Vault 只读抽取真实内容。
Calendar Today / Notes / To Do 三块以「当天日记 mtime」为缓存键：日记没改就复用
旧数据，改了才重读并总结（见 collectors/journal_cache.py）。
渲染结果写入 samples/preview_home.png，dash_server.py 每次请求会重新读该文件，
因此无需重启服务即可生效。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List

# 让 src/ 可被 import
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SRC_DIR)
sys.path.insert(0, SRC_DIR)

from dashboard_renderer import (                                       # noqa: E402
    DashboardRenderer, demo_snapshot, Todo, Weather, HourlyWeather, TokenUsage)
from collectors.weather import QWeatherClient, QWeatherError     # noqa: E402
from collectors import obsidian as obsidian_collector            # noqa: E402
from collectors import journal_cache                             # noqa: E402
from collectors import usage as usage_collector                  # noqa: E402

DEFAULT_OUT = os.path.join(PROJECT_ROOT, "samples", "preview_home.png")
WEATHER_CACHE_PATH = os.path.join(PROJECT_ROOT, "config", "weather_cache.json")
WEATHER_TTL = 3600  # 天气默认每 1 小时拉取一次

# Kindle 端上报的电量状态（由 dash_server.py 的 /report 接口写入）
KINDLE_STATUS_PATH = os.path.expanduser("~/.dash_server/kindle_status.json")


def _read_kindle_status():
    """读取 Kindle 上报的剩余电量。

    返回 (battery: Optional[int], charging: bool, reported_at: Optional[datetime])。
    文件不存在或解析失败时返回 (None, False, None)，由渲染器显示「看板端待上报电量」。
    """
    if not os.path.exists(KINDLE_STATUS_PATH):
        return None, False, None
    try:
        d = json.loads(Path(KINDLE_STATUS_PATH).read_text(encoding="utf-8"))
        batt = d.get("battery")
        charging = bool(d.get("charging", False))
        reported_at = None
        if d.get("reported_at"):
            try:
                reported_at = datetime.fromisoformat(d["reported_at"])
            except Exception:
                reported_at = None
        return batt, charging, reported_at
    except Exception:
        return None, False, None


# ------------------------------------------------------------------ 天气缓存
def _serialize_weather(weather: Weather, hourly: List[HourlyWeather]) -> dict:
    return {
        "weather": {
            "location": weather.location,
            "temp_now": weather.temp_now,
            "temp_high": weather.temp_high,
            "temp_low": weather.temp_low,
            "text": weather.text,
            "icon_code": weather.icon_code,
            "observed_at": weather.observed_at.isoformat()
                            if weather.observed_at else None,
            "stale": weather.stale,
        },
        "hourly": [
            {"hour": h.hour, "temp": h.temp, "icon_code": h.icon_code}
            for h in hourly
        ],
    }


def _deserialize_weather(d: dict):
    w = d["weather"]
    weather = Weather(
        location=w["location"],
        temp_now=w["temp_now"],
        temp_high=w["temp_high"],
        temp_low=w["temp_low"],
        text=w["text"],
        icon_code=w["icon_code"],
        observed_at=datetime.fromisoformat(w["observed_at"])
                    if w.get("observed_at") else None,
        stale=w.get("stale", False),
    )
    hourly = [HourlyWeather(hour=h["hour"], temp=h["temp"], icon_code=h["icon_code"])
              for h in d.get("hourly", [])]
    return weather, hourly


def get_weather_cached(client: QWeatherClient, ttl: int = WEATHER_TTL):
    """返回 (weather, hourly, from_cache)。

    - 缓存未过期（< ttl 秒）→ 直接复用，不联网。
    - 缓存过期或缺失 → 拉取并写回缓存；拉取失败则退化为过期缓存（标 stale）。
    """
    now = time.time()
    if os.path.exists(WEATHER_CACHE_PATH):
        try:
            c = json.loads(Path(WEATHER_CACHE_PATH).read_text(encoding="utf-8"))
            if now - c.get("fetched_at", 0) < ttl:
                weather, hourly = _deserialize_weather(c)
                return weather, hourly, True
        except (json.JSONDecodeError, OSError, KeyError):
            pass  # 缓存损坏 → 重新拉取

    try:
        weather, hourly = client.build_weather()
        try:
            os.makedirs(os.path.dirname(WEATHER_CACHE_PATH), exist_ok=True)
            Path(WEATHER_CACHE_PATH).write_text(
                json.dumps({"fetched_at": now, **_serialize_weather(weather, hourly)},
                           ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass  # 写缓存失败不影响本次渲染
        return weather, hourly, False
    except QWeatherError:
        # 拉取失败：尽量用过期缓存兜底
        if os.path.exists(WEATHER_CACHE_PATH):
            try:
                c = json.loads(Path(WEATHER_CACHE_PATH).read_text(encoding="utf-8"))
                weather, hourly = _deserialize_weather(c)
                weather.stale = True
                return weather, hourly, True
            except Exception:
                pass
        raise


def build(out_path: str = DEFAULT_OUT, force_journal: bool = False,
           weather_ttl: int = WEATHER_TTL):
    r = DashboardRenderer()
    snap = demo_snapshot()  # 其他面板先用演示数据

    # 主显示时钟 / 日历使用真实当前时间（每次重建都刷新，保证屏上时间实时）
    snap.now = datetime.now()

    try:
        client = QWeatherClient.from_config()
        weather, hourly, from_cache = get_weather_cached(client, ttl=weather_ttl)
        snap.weather = weather
        snap.hourly_weather = hourly
        snap.last_updated = datetime.now()
        tag = "缓存(1h内)" if from_cache else "实时拉取"
        print(f"[天气] {weather.location} {weather.temp_now}°C {weather.text} "
              f"(高{weather.temp_high}/低{weather.temp_low})  24h采样{len(hourly)}点 "
              f"[{tag}]")
    except QWeatherError as e:
        # 抓取失败：保留演示天气，但标记陈旧，避免误以为数据新鲜
        snap.weather.stale = True
        snap.weather.observed_at = None
        # 仍记录本次尝试时刻（如实反映“刚刚试过但没拿到新数据”）
        snap.last_updated = datetime.now()
        print(f"[天气] 抓取失败，使用缓存/演示数据：{e}", file=sys.stderr)

    print(f"[时间] 最近更新 {snap.last_updated:%H:%M:%S} | 生成时刻 {snap.now:%H:%M:%S}")

    # Kindle 剩余电量：由 dash_server 的 /report 接口写入的 JSON 读取
    batt, charging, reported_at = _read_kindle_status()
    snap.device.kindle_battery = batt
    snap.device.kindle_charging = charging
    snap.device.updated_at = reported_at
    if batt is None:
        print("[电量] 看板端尚未上报 Kindle 电量（dash.sh 需带上报逻辑）")
    else:
        chg = "充电中" if charging else "未充电"
        rp = f" 上报于 {reported_at:%H:%M:%S}" if reported_at else ""
        print(f"[电量] Kindle 剩余 {batt}% · {chg}{rp}")

    # 待办 / 笔记 / 今日日历：仅在当天日记变化时才重读并总结（缓存 by mtime）。
    # force_journal=True 时忽略缓存，强制重算（用于调试或采集逻辑更新后）。
    try:
        panels = journal_cache.get_journal_panels(force=force_journal, max_n=4)
        tag = "缓存命中·沿用旧数据" if panels.from_cache else "日记已更新·重新总结"

        if panels.todo:
            snap.todos = [
                Todo(title=t,
                     due_text=obsidian_collector._due_and_overdue(t)[0],
                     overdue=obsidian_collector._due_and_overdue(t)[1])
                for t in panels.todo
            ]
            print(f"[待办] 已接入 {len(snap.todos)} 条（来自今日日记待办）[{tag}]")
        else:
            # 空内容：不沿用 demo 数据，让渲染器整体隐藏 TO DO 面板
            snap.todos = []
            print("[待办] 今日日记无待办，面板留空")

        if panels.notes:
            snap.notes = panels.notes
            print(f"[笔记] 已接入 {len(snap.notes)} 条（当日复盘概要）[{tag}]")
        else:
            snap.notes = []   # 今日无复盘概要/叙事 → 留空，由渲染器显示「面板整体隐藏」
            print("[笔记] 今日无复盘概要，面板留空")

        if panels.make_summary:
            snap.make_summary = panels.make_summary
            print(f"[搞点什么] 已接入 {len(snap.make_summary)} 行"
                  f"（来自日记「搞点什么」区块概要）[{tag}]")
        else:
            # 空内容：不沿用 demo 数据，让渲染器整体隐藏「搞点什么」面板
            snap.make_summary = []
            print("[搞点什么] 日记无「搞点什么」概要，面板留空")
    except Exception as e:
        print(f"[Obsidian] 抽取失败，沿用演示数据：{e}", file=sys.stderr)

    # 用量：
    #   - MiniMax：mmx quota show，5h 窗口，含剩余百分比。
    #   - Codex ：codex app-server JSON-RPC 取 account/rateLimits/read 得剩余百分比；
    #             本地状态库 ~/.codex/state_*.sqlite 取历史总 token 作为补充。
    #   任一来源失败只影响自己。
    try:
        all_u = usage_collector.get_all_usage()

        def fmt_tokens(n):
            if n is None:
                return "?"
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M"
            if n >= 1_000:
                return f"{n / 1_000:.1f}K"
            return str(n)

        def fmt_reset(ts):
            """把 Unix 秒重置时间格式化为「约 XdYh 后重置」「约 XhYm 后重置」等。"""
            if ts is None:
                return ""
            secs = int(ts - time.time())
            if secs <= 0:
                return "即将重置"
            d, rem = divmod(secs, 86400)
            h, rem = divmod(rem, 3600)
            m = rem // 60
            if d > 0:
                if h > 0 and m > 0:
                    return f"约{d}d{h}h后重置"
                if h > 0:
                    return f"约{d}d{h}h后重置"
                return f"约{d}d后重置"
            if h > 0 and m > 0:
                return f"约{h}h{m}m后重置"
            if h > 0:
                return f"约{h}h后重置"
            return f"约{m}m后重置"

        snap.tokens = []
        for u in all_u:
            if u.provider == "Codex" and u.available and u.remaining_percent is not None:
                # app-server 给了剩余配额（5h 主窗口）；本地日志给累计 token。
                # 顶行与 MiniMax 同格式：剩X%·5h窗口；累计+重置倒计时放进度条下方。
                used = (100 - u.remaining_percent) / 100.0
                status = f"剩{u.remaining_percent}%"
                detail = f"{u.window_hours:.0f}h窗口" if u.window_hours else ""
                note_parts = []
                if u.lifetime_tokens:
                    note_parts.append(f"累计{fmt_tokens(u.lifetime_tokens)}")
                reset_txt = fmt_reset(u.resets_at)
                if reset_txt:
                    note_parts.append(reset_txt)
                if u.stale_age_min:
                    # 陈旧缓存：标注数据时间，避免当成实时额度误读
                    note_parts.append(f"{u.stale_age_min}分钟前")
                note = " · ".join(note_parts)
                # 周窗口（secondary）合并进同一 Codex 行内作为子条，不另起新行
                wk = {}
                if u.codex_weekly_remaining is not None:
                    wk = dict(
                        weekly_ratio=(100 - u.codex_weekly_remaining) / 100.0,
                        weekly_remaining=u.codex_weekly_remaining,
                        weekly_reset=fmt_reset(u.codex_weekly_resets_at),
                    )
                snap.tokens.append(TokenUsage(u.provider, used, status, detail, note,
                                             **wk))
            elif u.provider == "Codex" and u.available:
                # 拿不到剩余配额：主条显示「额度未知·5h窗口」+ 累计 token 备注；
                # 同时画一条「额度未知·周窗口」子条（通过 weekly_status 字段触发），
                # 让用户在网络不通时也能看到 Codex 在面板里仍占位、且识别出「周窗口
                # 也拿不到」——比只画一条更完整，避免误以为只丢了一个窗口。
                note_parts = []
                if u.lifetime_tokens:
                    note_parts.append(f"累计{fmt_tokens(u.lifetime_tokens)}")
                note_parts.append("后端不可达")
                snap.tokens.append(TokenUsage(
                    u.provider, 0.0, "额度未知", "5h窗口",
                    " · ".join(note_parts),
                    weekly_status="额度未知",
                ))
            elif u.available and u.remaining_percent is not None:
                # MiniMax：顶行剩X%·5h窗口；下方小字显示窗口重置倒计时。
                used = (100 - u.remaining_percent) / 100.0
                status = f"剩{u.remaining_percent}%"
                detail = f"{u.window_hours:.0f}h窗口" if u.window_hours else ""
                reset_ts = None
                if u.remains_ms is not None:
                    reset_ts = time.time() + u.remains_ms / 1000.0
                elif u.window_end_ms is not None:
                    reset_ts = u.window_end_ms / 1000.0
                note = fmt_reset(reset_ts)
                snap.tokens.append(TokenUsage(u.provider, used, status, detail, note))
            elif u.available:
                snap.tokens.append(TokenUsage(u.provider, 0.0, "OK", ""))
            else:
                snap.tokens.append(TokenUsage(
                    u.provider, 0.0,
                    "需终端" if "TUI" in u.raw_note else "UNAVAILABLE",
                    "TUI无法抓取" if "TUI" in u.raw_note else ""))

        mmx = next((u for u in all_u if u.provider == "MiniMax"), None)
        codex = next((u for u in all_u if u.provider == "Codex"), None)
        mmx_reset = None
        if mmx and mmx.remains_ms is not None:
            mmx_reset = time.time() + mmx.remains_ms / 1000.0
        elif mmx and mmx.window_end_ms is not None:
            mmx_reset = mmx.window_end_ms / 1000.0
        mmx_txt = f"剩{mmx.remaining_percent}%" if (mmx and mmx.remaining_percent is not None) else "不可用"
        if codex and codex.available and codex.remaining_percent is not None:
            stale = f" 陈旧{codex.stale_age_min}分钟" if codex.stale_age_min else ""
            codex_txt = (f"剩{codex.remaining_percent}% ({codex.window_hours:.0f}h窗口 "
                         f"累计{fmt_tokens(codex.lifetime_tokens)} "
                         f"{fmt_reset(codex.resets_at)}{stale})")
            if codex.codex_weekly_remaining is not None:
                wk_h = codex.codex_weekly_window_hours
                wk_h_txt = f"{wk_h:.0f}h" if wk_h is not None else "周窗口"
                wk_reset = fmt_reset(codex.codex_weekly_resets_at)
                codex_txt += (f" | 周窗口剩{codex.codex_weekly_remaining}% "
                              f"({wk_h_txt}{' · ' + wk_reset if wk_reset else ''})")
        elif codex and codex.available:
            codex_txt = f"额度未知 (累计{fmt_tokens(codex.lifetime_tokens)} 后端不可达)"
        else:
            codex_txt = "可用" if (codex and codex.available) else "需终端"
        if mmx_reset:
            mmx_txt += f" ({fmt_reset(mmx_reset)})"
        print(f"[用量] MiniMax {mmx_txt} | Codex {codex_txt}")
    except Exception as e:
        print(f"[用量] 采集失败，沿用演示数据：{e}", file=sys.stderr)

    r.save(snap, out_path)
    print(f"[渲染] 已生成 {out_path}")
    return out_path


if __name__ == "__main__":
    args = sys.argv[1:]
    out = DEFAULT_OUT
    force = False
    if "--force" in args:
        force = True
        args = [a for a in args if a != "--force"]
    if args:
        out = args[0]
    build(out, force_journal=force)
