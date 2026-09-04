"""用量采集器：从 CLI / 本地日志拉取各服务商的用量快照。

已实测可用的来源：
  - MiniMax : ``mmx quota show --output json``
      返回每个模型的滚动配额窗口。``general`` 模型的窗口恰好是 5 小时
      （end_time - start_time == 5h），含 current_interval_usage_count /
      current_interval_remaining_percent / remains_time 等字段。
  - Codex   : 解析本地状态库 ``~/.codex/state_5.sqlite`` 的 ``threads`` 表。
      每行线程的 ``tokens_used`` 是该线程累计消耗的 token；聚合即得到
      全量用量，再按 ``updated_at_ms`` 落在最近 5h 内的线程求和，得到
      「近 5h 用量」。无需调用 TUI、无需联网，headless auto-build 下可用。

设计原则：任一来源失败只影响自己，不拖垮整块面板；拿不到真实数据绝不伪造。
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class ProviderUsage:
    """单个服务商的用量快照。"""
    provider: str
    available: bool
    window_hours: float = 0.0          # 滚动窗口时长（小时），MiniMax general=5
    used_count: Optional[int] = None   # 当前窗口已用次数 / 额度
    total_count: Optional[int] = None  # 当前窗口总额度（可能为 None）
    remaining_percent: Optional[int] = None   # 剩余百分比 0-100
    remains_ms: Optional[int] = None   # 当前窗口剩余毫秒数
    weekly_remaining_percent: Optional[int] = None
    # Codex 周窗口（secondary）：与 5h 主窗口同出一处 rateLimits，只是没在面板展示
    codex_weekly_remaining: Optional[int] = None
    codex_weekly_window_hours: Optional[float] = None
    codex_weekly_resets_at: Optional[int] = None
    window_start_ms: Optional[int] = None
    window_end_ms: Optional[int] = None
    used_tokens: Optional[int] = None       # 近窗口内消耗的 token（Codex 本地日志）
    lifetime_tokens: Optional[int] = None   # 累计消耗 token（Codex 本地日志）
    resets_at: Optional[int] = None         # 窗口重置时间（Unix 秒，Codex app-server）
    stale_age_min: Optional[int] = None     # 数据陈旧时长（分钟）。非 None 表示
                                            # app-server 拉取失败、当前值是旧缓存
    raw_note: str = ""                 # 不可用时的人类可读原因 / 摘要
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def summary_line(self) -> str:
        """给 Kindle 面板用的单行紧凑摘要。"""
        if not self.available:
            return f"{self.provider}: 不可用"
        parts = []
        if self.remaining_percent is not None:
            parts.append(f"剩{self.remaining_percent}%")
        if self.used_count is not None:
            if self.total_count:
                parts.append(f"{self.used_count}/{self.total_count}")
            else:
                parts.append(f"用{self.used_count}")
        if self.window_hours:
            parts.append(f"{self.window_hours:.0f}h窗")
        return f"{self.provider}: " + " ".join(parts)


# --------------------------------------------------------------------------- #
# MiniMax
# --------------------------------------------------------------------------- #
def _resolve_exe(name: str) -> str:
    """Windows 上 CLI 多为无扩展名 sh 脚本（mmx / codex），subprocess 直接
    传名会 FileNotFoundError，需先 which 解析成完整路径。"""
    exe = shutil.which(name)
    return exe or name


def get_minimax_usage() -> ProviderUsage:
    """调用 ``mmx quota show --output json``，解析 general 模型的 5h 窗口。"""
    exe = _resolve_exe("mmx")
    try:
        proc = subprocess.run(
            [exe, "quota", "show", "--output", "json", "--no-color"],
            capture_output=True, text=True, encoding="utf-8", timeout=90,
        )
    except Exception as e:
        return ProviderUsage(provider="MiniMax", available=False,
                             raw_note=f"调用失败：{e}")

    if proc.returncode != 0 or not proc.stdout.strip():
        return ProviderUsage(provider="MiniMax", available=False,
                             raw_note=f"空输出(rc={proc.returncode})：{proc.stderr[:160]}")

    try:
        obj = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return ProviderUsage(provider="MiniMax", available=False,
                             raw_note=f"JSON 解析失败：{e}")

    models = obj.get("model_remains") or []
    if not models:
        return ProviderUsage(provider="MiniMax", available=False,
                             raw_note="返回无 model_remains")

    # 优先取 general（5h 窗口），否则取第一个
    m = next((x for x in models if x.get("model_name") == "general"), models[0])
    start = m.get("start_time")
    end = m.get("end_time")
    wh = round((end - start) / 3_600_000, 2) if (start and end) else 0.0
    return ProviderUsage(
        provider="MiniMax",
        available=True,
        window_hours=wh,
        used_count=m.get("current_interval_usage_count"),
        total_count=m.get("current_interval_total_count"),
        remaining_percent=m.get("current_interval_remaining_percent"),
        remains_ms=m.get("remains_time"),
        weekly_remaining_percent=m.get("current_weekly_remaining_percent"),
        window_start_ms=start,
        window_end_ms=end,
        raw_note=f"model={m.get('model_name')}",
    )


# --------------------------------------------------------------------------- #
# Codex：app-server JSON-RPC + 本地状态库双源
# --------------------------------------------------------------------------- #
# Codex 用量分两块：
#   1. 剩余配额 / 窗口信息：来自 ``codex app-server`` 的 JSON-RPC 接口
#      ``account/rateLimits/read``。这是 ``codex usage`` TUI 背后的同一来源。
#   2. 历史总 token：来自本地状态库 ``~/.codex/state_5.sqlite`` 的 threads 表，
#      只读，无外部依赖，失败不影响配额信息。
#
# app-server 启动需要 2-4 秒，因此结果缓存 5 分钟；本地日志解析始终实时。
# --------------------------------------------------------------------------- #
_CODEX_CACHE_FILE = os.path.join(os.path.expanduser("~"), ".dash_server",
                                 "codex_usage_cache.json")
_CODEX_CACHE_TTL_SECONDS = 300
# app-server 拉取失败时的宽容期：30 分钟内可复用上一次成功值并标注「陈旧」；
# 超过则判定为「额度未知」——5h 窗口可能已重置，拿过期百分比冒充实时额度会严重误导。
_CODEX_STALE_GRACE_SECONDS = 1800


def _find_codex_state_db() -> Optional[str]:
    """定位当前 Codex 状态库。"""
    base = os.path.join(os.path.expanduser("~"), ".codex")
    if not os.path.isdir(base):
        return None
    candidates = sorted(glob.glob(os.path.join(base, "state_*.sqlite")),
                        key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0] if candidates else None


def _get_codex_local_log_usage() -> tuple[int, int, int]:
    """从本地状态库返回 (lifetime_tokens, recent_5h_tokens, active_threads)。"""
    db = _find_codex_state_db()
    if not db:
        return 0, 0, 0
    try:
        con = sqlite3.connect(db, timeout=5)
        con.execute("PRAGMA query_only = 1")
        rows = con.execute(
            "SELECT tokens_used, updated_at_ms FROM threads"
        ).fetchall()
        con.close()
    except Exception:
        return 0, 0, 0

    now_ms = time.time() * 1000.0
    window_ms = 5 * 3600 * 1000.0
    lifetime = 0
    recent = 0
    active = 0
    for used, updated in rows:
        used = used or 0
        lifetime += used
        if updated is not None and (now_ms - updated) <= window_ms:
            recent += used
            active += 1
    return lifetime, recent, active


def _call_codex_app_server_rate_limits(timeout: int = 30) -> Optional[Dict[str, Any]]:
    """启动 ``codex app-server`` 并通过 JSON-RPC 读取 ``account/rateLimits/read``。

    只拉取 rateLimits，3 秒左右可完成；不拉 account/usage 避免多花 20 多秒。

    代理：codex CLI 默认不读 ``HTTPS_PROXY``，而 ``chatgpt.com`` 在国内常被屏蔽，
    导致 ``error sending request for url https://chatgpt.com/backend-api/wham/usage``。
    本函数把 ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``ALL_PROXY`` 显式透传给子进程环境，
    配置后即可恢复 rateLimits 拉取。
    """
    exe = shutil.which("codex")
    if not exe:
        return None

    # 把系统代理透传给 codex 子进程（避免 chatgpt.com 不可达）
    env = os.environ.copy()
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy"):
        v = os.environ.get(k)
        if v:
            env[k] = v

    proc = subprocess.Popen(
        [exe, "app-server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
        env=env,
    )

    def send(msg: dict) -> None:
        line = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
        proc.stdin.write(line + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    # 后台排空 stderr，防止管道阻塞
    stderr_drain = []
    def drain_stderr() -> None:
        try:
            for line in proc.stderr:
                stderr_drain.append(line)
                if len(stderr_drain) > 200:
                    break
        except Exception:
            pass
    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()

    try:
        send({
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {"name": "kindle_usage_agent",
                                "title": "Kindle Dashboard Usage Agent",
                                "version": "1.0.0"}
            }
        })
        send({"method": "initialized", "params": {}})
        send({"method": "account/rateLimits/read", "id": 1})

        deadline = time.time() + timeout
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == 1:
                if msg.get("error"):
                    raise RuntimeError(f"account/rateLimits/read error: {msg['error']}")
                return msg.get("result")
        raise TimeoutError("等待 codex app-server 响应超时")
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            t.join(timeout=2)
        except Exception:
            pass


def _read_codex_cache() -> Optional[Dict[str, Any]]:
    try:
        if not os.path.exists(_CODEX_CACHE_FILE):
            return None
        with open(_CODEX_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        ts = data.get("fetched_at_ts", 0)
        if time.time() - ts > _CODEX_CACHE_TTL_SECONDS:
            return None
        return data
    except Exception:
        return None


def _read_codex_cache_any() -> Optional[Dict[str, Any]]:
    """无视 TTL 读取上一次成功的配额快照，附带陈旧时长（秒）。

    供 app-server 拉取失败时降级使用；是否复用由调用方按
    ``_CODEX_STALE_GRACE_SECONDS`` 决定。
    """
    try:
        if not os.path.exists(_CODEX_CACHE_FILE):
            return None
        with open(_CODEX_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        ts = data.get("fetched_at_ts", 0)
        if not ts:
            return None
        data["_age_seconds"] = max(0, int(time.time() - ts))
        return data
    except Exception:
        return None


def _write_codex_cache(data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(_CODEX_CACHE_FILE), exist_ok=True)
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fetched_at_ts": time.time(),
            **data,
        }
        with open(_CODEX_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _fetch_codex_rate_limits() -> Optional[Dict[str, Any]]:
    """带缓存的 rateLimits 读取，三级降级：

    1. 5 分钟内的新鲜缓存，直接用；
    2. 调 app-server，成功则写入缓存；
    3. 失败时复用 30 分钟内的旧缓存并标记 ``stale``（面板标注陈旧时长）；
       超过 30 分钟则返回 None（5h 窗口可能已重置，旧值会严重误导）。
    """
    cached = _read_codex_cache()
    if cached and "remaining_percent" in cached:
        return cached

    result = None
    try:
        result = _call_codex_app_server_rate_limits(timeout=30)
    except Exception:
        result = None

    if not result:
        stale = _read_codex_cache_any()
        if stale and "remaining_percent" in stale:
            age = stale.get("_age_seconds", 0)
            if age <= _CODEX_STALE_GRACE_SECONDS:
                stale["stale"] = True
                stale["stale_age_min"] = max(1, age // 60)
                return stale
        return None

    rate_limits = result.get("rateLimits") or {}
    primary = rate_limits.get("primary") or {}
    secondary = rate_limits.get("secondary") or {}
    used = primary.get("usedPercent")
    if used is None:
        return None
    payload = {
        "remaining_percent": max(0, min(100, 100 - int(used))),
        "used_percent": int(used),
        "window_hours": round(primary.get("windowDurationMins", 0) / 60.0, 2),
        "secondary_percent": secondary.get("usedPercent"),
        "secondary_window_hours": round(secondary.get("windowDurationMins", 0) / 60.0, 2),
        "secondary_resets_at": secondary.get("resetsAt"),
        "plan_type": rate_limits.get("planType") or "unknown",
        "resets_at": primary.get("resetsAt"),
    }
    _write_codex_cache(payload)
    return payload


def get_codex_usage() -> ProviderUsage:
    """Codex 用量：本地状态库给 token 累计，app-server JSON-RPC 给剩余配额。

    常见失败：``chatgpt.com`` 在国内网络下被屏蔽，codex 子进程拿不到
    ``HTTPS_PROXY`` 导致 ``error sending request for url ...wham/usage``。
    修复：在子进程 env 里显式透传 ``HTTPS_PROXY`` 等代理变量。
    """
    lifetime, recent, active = _get_codex_local_log_usage()

    limits: Optional[Dict[str, Any]] = None
    note = "app-server 不可达（chatgpt.com 不通）"
    try:
        limits = _fetch_codex_rate_limits()
    except Exception as e:
        note = f"chatgpt.com 不可达：{e}"

    if limits:
        window_hours = limits.get("window_hours") or 0.0
        stale_min = limits.get("stale_age_min")
        raw_note = (
            f"plan={limits.get('plan_type')} "
            f"secondary={limits.get('secondary_percent')}%"
            f"| active_threads={active}"
        )
        if stale_min:
            raw_note = f"陈旧数据({stale_min}分钟前) " + raw_note
        return ProviderUsage(
            provider="Codex",
            available=True,
            window_hours=window_hours,
            remaining_percent=limits.get("remaining_percent"),
            used_count=limits.get("used_percent"),
            used_tokens=recent,
            lifetime_tokens=lifetime,
            resets_at=limits.get("resets_at"),
            stale_age_min=stale_min,
            raw_note=raw_note,
            codex_weekly_remaining=(100 - int(limits["secondary_percent"]))
                if limits.get("secondary_percent") is not None else None,
            codex_weekly_window_hours=limits.get("secondary_window_hours"),
            codex_weekly_resets_at=limits.get("secondary_resets_at"),
        )

    # 拿不到剩余配额：绝不拿 token 累计去冒充「已用量百分比」（本地日志没有
    # 配额上限，算不出百分比）。remaining_percent 留空，由面板显示「额度未知」，
    # 累计 token 仅作为补充小字。
    if lifetime:
        return ProviderUsage(
            provider="Codex",
            available=True,
            window_hours=5.0,
            used_tokens=recent,
            lifetime_tokens=lifetime,
            raw_note="chatgpt.com 不可达（设 HTTPS_PROXY 即可恢复额度），剩余额度未知",
        )

    return ProviderUsage(
        provider="Codex",
        available=False,
        raw_note=note if limits is None else "未获取到 Codex 用量数据",
    )


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #
def get_all_usage() -> List[ProviderUsage]:
    return [get_minimax_usage(), get_codex_usage()]


if __name__ == "__main__":
    import os
    os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))  # 项目根
    for u in get_all_usage():
        print(u.summary_line())
        if not u.available:
            print(f"    原因: {u.raw_note}")
