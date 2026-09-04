#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kindle Dashboard 本地 HTTP 服务。

电脑端常驻，负责把最新渲染好的 1072x1448 PNG 暴露给同一 wifi 内的 Kindle。
Kindle 唤醒后 curl 拉图：

    curl -o /tmp/dash.png http://<电脑IP>:8765/dash.png?t=<token>

用法：
    python dash_server.py --image samples/preview_home.png --port 8765
    python dash_server.py --image samples/preview_home.png --token my-secret-token

Token 默认自动生成 43 字符 URL-safe 随机串，并写入
    C:/Users/<user>/.dash_server/token
方便 Kindle 端脚本读取同一份 token。
"""

import argparse
import json
import mimetypes
import secrets
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8765
TOKEN_DIR = Path.home() / ".dash_server"
TOKEN_FILE = TOKEN_DIR / "token"
# Kindle 端通过 /report 接口上报的电量状态文件（build_dashboard.py 会读它）
KINDLE_STATUS_PATH = TOKEN_DIR / "kindle_status.json"


def load_kindle_status() -> dict:
    """读取 Kindle 上次上报的电量状态；文件缺失或损坏返回空字典。"""
    if not KINDLE_STATUS_PATH.exists():
        return {}
    try:
        return json.loads(KINDLE_STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_kindle_status(status: dict) -> None:
    try:
        TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        KINDLE_STATUS_PATH.write_text(
            json.dumps(status, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# 进程内缓存（与磁盘文件保持一致，避免每次请求都读盘）
kindle_status = load_kindle_status()

# 可选：每次拉图前自动重建看板（需 src/build_dashboard.py）
try:
    from build_dashboard import build as _build_dashboard
    _BUILD_AVAILABLE = True
except Exception:  # 缺少依赖时静默降级，仍按原逻辑服务静态文件
    _BUILD_AVAILABLE = False


def log(msg: str):
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)


def load_or_create_token(token_arg: str | None) -> str:
    if token_arg:
        return token_arg
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    t = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    return t


def make_handler(image_path: Path, token: str, auto_build: bool = False,
                 build_ttl: int = 600):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # 使用自己的日志格式
            pass

        def _maybe_rebuild(self):
            """auto-build 模式：每次拉图都重建，保证屏上时间实时刷新。

            天气联网拉取由 build_dashboard 内部的 1 小时缓存节流，
            日记抽取由 mtime 缓存节流，因此「每次重建」只重渲染 PNG，
            不会造成频繁联网。
            """
            if not auto_build or not _BUILD_AVAILABLE:
                return
            try:
                _build_dashboard(str(image_path))
                size = image_path.stat().st_size if image_path.exists() else 0
                log(f"{self.client_address[0]} auto-rebuilt dashboard ({size} bytes)")
            except Exception as e:
                log(f"{self.client_address[0]} auto-rebuild failed: {e}")

        def _unauthorized(self):
            self.send_response(403)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Forbidden: missing or invalid token")

        def _ok_json(self, data: dict):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_token(self) -> bool:
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            pairs = {}
            for part in query.split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    pairs[k] = v
            return pairs.get("t") == token

        def _handle_report(self):
            """Kindle 端上报电量：/report?t=<token>&b=<0-100>&c=<0|1>。

            支持 GET（Kindle curl 简单拼接）与 POST。写入磁盘后由 build_dashboard
            读取，渲染进 SYS STATUS 面板。
            """
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            pairs = {}
            for part in query.split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    pairs[k] = v
            if pairs.get("t") != token:
                self._unauthorized()
                return
            try:
                batt_raw = pairs.get("b", "")
                if batt_raw in ("", "None", "null"):
                    batt = None
                else:
                    batt = int(batt_raw)
                    batt = max(0, min(100, batt))
                charging = pairs.get("c", "") in ("1", "y", "Y", "true", "True")
            except Exception:
                batt, charging = None, False
            kindle_status["battery"] = batt
            kindle_status["charging"] = charging
            kindle_status["reported_at"] = datetime.now().isoformat(timespec="seconds")
            save_kindle_status(kindle_status)
            log(f"{self.client_address[0]} battery report: {batt}% charging={charging}")
            self._ok_json({"ok": True, "battery": batt, "charging": charging})

        def do_POST(self):
            client = self.client_address[0]
            path = self.path.split("?", 1)[0]
            if path == "/report":
                self._handle_report()
                return
            log(f"{client} POST {self.path} -> 404")
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not Found")

        def do_GET(self):
            client = self.client_address[0]
            path = self.path.split("?", 1)[0]

            if path == "/report":
                self._handle_report()
                return

            if path == "/health":
                log(f"{client} GET {self.path} -> health")
                self._ok_json({
                    "ok": True,
                    "image": str(image_path),
                    "image_exists": image_path.exists(),
                    "image_size": image_path.stat().st_size if image_path.exists() else 0,
                    "token_length": len(token),
                    "kindle_battery": kindle_status.get("battery"),
                    "kindle_charging": kindle_status.get("charging"),
                    "kindle_reported_at": kindle_status.get("reported_at"),
                })
                return

            if path != "/dash.png":
                log(f"{client} GET {self.path} -> 404")
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Not Found")
                return

            if not self._check_token():
                log(f"{client} GET {self.path} -> 403")
                self._unauthorized()
                return

            self._maybe_rebuild()
            if not image_path.exists():
                log(f"{client} GET {self.path} -> 404 (image missing)")
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"Image not found: {image_path}".encode("utf-8"))
                return

            data = image_path.read_bytes()
            mime, _ = mimetypes.guess_type(str(image_path))
            self.send_response(200)
            self.send_header("Content-Type", mime or "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("X-Timestamp", str(time.time()))
            self.end_headers()
            self.wfile.write(data)
            log(f"{client} GET {self.path} -> 200 ({len(data)} bytes)")

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Kindle Dashboard HTTP server")
    ap.add_argument("--image", required=True, type=Path, help="要服务的 PNG 文件路径")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）")
    ap.add_argument("--token", default=None, help="访问 token；默认从 ~/.dash_server/token 读取或自动生成")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0，让同 wifi 设备访问）")
    ap.add_argument("--auto-build", action="store_true",
                    help="每次拉图前自动重建看板（时间实时刷新；天气/日记由各自缓存节流）")
    ap.add_argument("--build-ttl", type=int, default=0,
                    help="（已弃用，保留兼容）重建间隔；当前每次拉图都重建")
    args = ap.parse_args()

    token = load_or_create_token(args.token)
    handler = make_handler(args.image, token,
                           auto_build=args.auto_build, build_ttl=args.build_ttl)

    server = ThreadingHTTPServer((args.host, args.port), handler)
    log(f"Kindle Dashboard server starting")
    log(f"  URL:    http://<本机IP>:{args.port}/dash.png?t={token}")
    log(f"  Image:  {args.image}")
    log(f"  Health: http://<本机IP>:{args.port}/health")
    log(f"  Report: http://<本机IP>:{args.port}/report?t={token}&b=<0-100>&c=<0|1>")
    log(f"  Token file: {TOKEN_FILE}")
    log(f"  Auto-build: {'ON (每次拉图重建，时间实时；天气1h/日记mtime缓存)' if (args.auto_build and _BUILD_AVAILABLE) else 'off'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
