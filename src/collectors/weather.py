# -*- coding: utf-8 -*-
"""
和风天气（QWeather）采集器。

仅用标准库（urllib + gzip），无第三方依赖。
从 config/weather.json 读取 api_host / api_key / location_id / location_name。

API Host 是每个 QWeather 项目独立的子域（如 hw5g7f5933.re.qweatherapi.com），
写在控制台「我的项目 → 调用地址」里，不能用默认 api/devapi.qweather.com 替代。

返回结构直接对接 dashboard_renderer 的 Weather / HourlyWeather：
- now.icon / hourly.icon 是 QWeather 天气代码字符串（如 "100" / "104"），
  与 assets/weather_icons/<code>.png 一一对应。
"""

from __future__ import annotations

import gzip
import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

# 项目根目录（src/collectors/weather.py -> ../../）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "weather.json"

_BEIJING = timezone(timedelta(hours=8))


@dataclass
class WeatherNow:
    obs_time: datetime
    temp: int
    feels_like: int
    text: str
    icon_code: str
    humidity: int
    wind_dir: str
    wind_scale: str


@dataclass
class HourlyPoint:
    time: datetime
    temp: int
    text: str
    icon_code: str


@dataclass
class DailyPoint:
    date: str
    temp_max: int
    temp_min: int
    text_day: str
    icon_code: str


class QWeatherError(RuntimeError):
    pass


def _http_get_json(host: str, key: str, path: str, lang: str = "zh") -> dict:
    url = f"https://{host}{path}" + (f"&key={key}" if "?" in path else f"?key={key}")
    if "lang=" not in url:
        url += f"&lang={lang}"
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        raise QWeatherError(f"HTTP {e.code} from {url}: {body}") from e
    except Exception as e:  # 网络/超时
        raise QWeatherError(f"请求失败 {url}: {e}") from e

    # 某些节点仍可能 gzip，兜底解压
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise QWeatherError(f"响应不是合法 JSON: {raw[:200]}") from e

    if data.get("code") not in ("200", 200):
        raise QWeatherError(f"API 错误 code={data.get('code')} "
                           f"message={data.get('message') or data.get('error')}")
    return data


def _parse_time(s: str) -> datetime:
    """解析 QWeather 的 '2026-09-04T03:05+08:00' 为带时区的 datetime。"""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # 去掉冒号时区偏移以兼容 fromisoformat
    if "+" in s or "-" in s[10:]:
        base, _, tz = s.partition("+") if "+" in s else (s.partition("-")[0], "-", s.partition("-")[2])
        if tz and ":" not in tz:
            pass
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # 退化为本地时间
        return datetime.now(_BEIJING)


class QWeatherClient:
    def __init__(self, api_host: str, api_key: str, location_id: str,
                 location_name: str = "", lang: str = "zh"):
        self.host = api_host
        self.key = api_key
        self.location_id = location_id
        self.location_name = location_name or "上海黄浦区"
        self.lang = lang

    @classmethod
    def from_config(cls, path: Optional[Path] = None) -> "QWeatherClient":
        cfg = json.loads((path or CONFIG_PATH).read_text(encoding="utf-8"))
        return cls(
            api_host=cfg["api_host"],
            api_key=cfg["api_key"],
            location_id=cfg.get("location_id") or cfg.get("legacy_location_id", "101020400"),
            location_name=cfg.get("location_name", "上海黄浦区"),
            lang=cfg.get("lang", "zh"),
        )

    def fetch_now(self) -> WeatherNow:
        data = _http_get_json(self.host, self.key,
                              f"/v7/weather/now?location={self.location_id}", self.lang)
        n = data["now"]
        return WeatherNow(
            obs_time=_parse_time(n["obsTime"]),
            temp=int(n["temp"]),
            feels_like=int(n["feelsLike"]),
            text=n.get("text", ""),
            icon_code=str(n.get("icon", "")),
            humidity=int(n.get("humidity", 0)),
            wind_dir=n.get("windDir", ""),
            wind_scale=n.get("windScale", ""),
        )

    def fetch_24h(self) -> List[HourlyPoint]:
        data = _http_get_json(self.host, self.key,
                              f"/v7/weather/24h?location={self.location_id}", self.lang)
        out = []
        for h in data.get("hourly", []):
            out.append(HourlyPoint(
                time=_parse_time(h["fxTime"]),
                temp=int(h["temp"]),
                text=h.get("text", ""),
                icon_code=str(h.get("icon", "")),
            ))
        return out

    def fetch_3d(self) -> List[DailyPoint]:
        data = _http_get_json(self.host, self.key,
                              f"/v7/weather/3d?location={self.location_id}", self.lang)
        out = []
        for d in data.get("daily", []):
            out.append(DailyPoint(
                date=d.get("fxDate", ""),
                temp_max=int(d["tempMax"]),
                temp_min=int(d["tempMin"]),
                text_day=d.get("textDay", ""),
                icon_code=str(d.get("iconDay", "")),
            ))
        return out

    def build_weather(self):
        """返回 (Weather, List[HourlyWeather]) 供渲染器使用。"""
        from dashboard_renderer import Weather, HourlyWeather  # 延迟导入避免循环

        now = self.fetch_now()
        hourly = self.fetch_24h()
        daily = self.fetch_3d()

        hi = lo = None
        if daily:
            hi = daily[0].temp_max
            lo = daily[0].temp_min

        weather = Weather(
            location=self.location_name,
            temp_now=now.temp,
            temp_high=hi,
            temp_low=lo,
            text=now.text,
            icon_code=now.icon_code,
            observed_at=now.obs_time,
        )

        # 24h 面板展示 6 个采样点（每 4 小时一个，覆盖全天）
        n = len(hourly)
        sample_idx = [i * (n // 6) for i in range(6)] if n >= 6 else list(range(n))
        hourly_out = [
            HourlyWeather(
                hour=hourly[i].time.strftime("%H:00"),
                temp=hourly[i].temp,
                icon_code=hourly[i].icon_code,
            )
            for i in sample_idx if i < n
        ]
        return weather, hourly_out


if __name__ == "__main__":
    client = QWeatherClient.from_config()
    print(f"位置: {client.location_name} ({client.location_id})  host={client.host}")
    w = client.fetch_now()
    print(f"实时: {w.temp}°C {w.text} 体感{w.feels_like}° 湿度{w.humidity}% "
          f"风{w.wind_dir}{w.wind_scale}级  图标{w.icon_code}")
    h = client.fetch_24h()
    print(f"24h 共 {len(h)} 条，首条 {h[0].time} {h[0].temp}° {h[0].text}")
    d = client.fetch_3d()
    if d:
        print(f"今日 {d[0].temp_min}~{d[0].temp_max}° {d[0].text_day}")
