# -*- coding: utf-8 -*-
"""
今日日记面板缓存：Calendar Today / Notes / To Do 这三块只在「当天日记文件
内容变化（mtime）」时才重新读日记并总结，否则复用上一次的结果（旧数据）。

设计（对应需求）：
- 缓存键：当天日记 01_Daily/<YYYY-MM-DD>.md 的 mtime（最近修改时间）。
- mtime 未变 → 直接返回上次总结结果，不重新扫描 Vault。
- mtime 变了 → 唤起「agent」（run_journal_agent）重读日记，重新总结三块并写回缓存。

说明：这里的「agent」是离线确定性抽取函数（读当天日记 → 抽待办 / 笔记 / 去重），
不依赖外部 LLM，保证后台 auto-build 永不因网络/密钥失败。如需真正的 AI 摘要，
可把 run_journal_agent 内部替换为 LLM 调用，对外接口不变。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from . import obsidian as obsidian_collector

_PROJECT_ROOT = Path(__file__).resolve().parents[2]   # src/collectors/ -> 项目根
_CACHE_PATH = _PROJECT_ROOT / "config" / "journal_cache.json"
_MINIMAX_CFG = _PROJECT_ROOT / "config" / "minimax.json"

_DAILY_GLOB = "01_Daily"
_CACHE_SCHEMA = 2   # 缓存结构版本（随字段变更递增）


@dataclass
class JournalPanels:
    make_summary: List[str] = field(default_factory=list)     # 今日日记「🛠️ 搞点什么」区块的「概要」
    notes: List[str] = field(default_factory=list)            # 当日复盘「概要」（MiniMax 仅兜底）
    todo: List[str] = field(default_factory=list)             # 当天日记「待办」区
    source: str = ""                                          # 当天日记绝对路径
    computed_at: Optional[str] = None                         # 上次总结时刻
    from_cache: bool = False                                  # 本次是否命中缓存


def _today_diary(vault: Optional[str], day: date) -> Optional[Path]:
    root = Path(vault or obsidian_collector._DEFAULT_VAULT)
    d = root / _DAILY_GLOB / f"{day.isoformat()}.md"
    return d if d.exists() else None


def _norm(title: str) -> str:
    """归一化用于去重比较（去空白 + 小写）。"""
    return "".join(title.split()).lower()


def _resolve_exe(name: str) -> str:
    """Windows 上 npm 全局装的 CLI（如 mmx）多为无扩展名 sh 脚本，
    CreateProcess 直接按文件名找不到；经 shutil.which 解析成 mmx.cmd 等真实可执行。"""
    exe = shutil.which(name)
    if exe:
        return exe
    if os.name == "nt":
        for ext in (".cmd", ".ps1", ".bat", ".exe"):
            e = shutil.which(name + ext)
            if e:
                return e
    return name


def _extract_text_from_mmx(output: str) -> Optional[str]:
    """mmx text chat 输出 JSON：{"content":[{"text":"..."}], ...}。
    解析出真正的总结文本；非 JSON 时若纯文本也接受。"""
    try:
        obj = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return output.strip() or None
    if isinstance(obj, dict):
        content = obj.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and first.get("text"):
                return first["text"].strip()
        if obj.get("text"):
            return obj["text"].strip()
    return None


def _extract_prose(diary_text: str) -> str:
    """剔除 frontmatter/标题/callout(待办)与待办项，保留叙事正文供 MiniMax 总结，
    避免 Notes 与 Calendar Today 的待办重复。"""
    t = re.sub(r"^---.*?---\n", "", diary_text, flags=re.S)
    t = re.sub(r">\[![^\]]*\]", "", t)            # >[!todo] 标记
    t = re.sub(r">>[^\n]*\n?", "", t)             # >> 嵌套内容
    t = re.sub(r">[^\n]*\n?", "", t)             # > 引用行
    t = re.sub(r"-\s*\[[ x]\][^\n]*\n?", "", t)  # 待办项
    t = re.sub(r"#{1,6}\s*[^\n]*\n?", "", t)     # 标题
    t = re.sub(r"[*_~`>#]", "", t)
    t = t.replace("今日核心", "").replace("待办", "")
    return re.sub(r"\s+", " ", t).strip()


def _extract_make_summary(diary_text: str) -> List[str]:
    """从当天日记的「🛠️ 搞点什么」区块抽取『概要』（确定性，无需联网）。

    锁定 CORE_MAKE_SUMMARY_START … CORE_MAKE_SUMMARY_END 标记之间的内容，
    取其中 `**概要**：…` 的行（可多行），去掉标记前缀后返回。
    无该区块 / 无概要 → 返回空列表。
    """
    m = re.search(
        r"CORE_MAKE_SUMMARY_START\s*-->(.*?)<!--\s*CORE_MAKE_SUMMARY_END",
        diary_text, flags=re.S)
    if not m:
        return []
    block = m.group(1)
    out: List[str] = []
    for ln in block.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        # 去掉 **概要**： / **概要:** 前缀
        ln = re.sub(r"^\*\*概要\*\*\s*[:：]?\s*", "", ln).strip().strip("*").strip()
        if ln:
            out.append(ln)
    return out[:3]


def _extract_review_summary(diary_text: str) -> List[str]:
    """从当天日记的「今日复盘」区块抽取『概要』（确定性，无需联网）。

    锁定 REVIEW_SUMMARY_START … REVIEW_SUMMARY_END 标记之间的内容，
    取其中 `**概要**：…` 的行（可多行），去掉标记前缀后返回。
    无复盘区块 / 无概要 → 返回空列表（由上层回退 MiniMax 摘要）。
    """
    m = re.search(
        r"REVIEW_SUMMARY_START\s*-->(.*?)<!--\s*REVIEW_SUMMARY_END",
        diary_text, flags=re.S)
    if not m:
        return []
    block = m.group(1)
    out: List[str] = []
    for ln in block.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        # 去掉 **概要**： / **概要:** 前缀
        ln = re.sub(r"^\*\*概要\*\*\s*[:：]?\s*", "", ln).strip().strip("*").strip()
        if ln:
            out.append(ln)
    return out[:3]


def summarize_notes_with_minimax(diary_text: str) -> Optional[str]:
    """用 MiniMax CLI 把今日日记总结成 1-2 句笔记。

    从 config/minimax.json 读取调用配置：
      {
        "enabled": true,
        "command": ["mmx", "text", "chat", "--message"],   # 末位是 --message
        "prompt":  "用最多两句、换行分隔，总结以下今日日记核心要点：\n\n{text}",
        "timeout": 90
      }
    command 为列表时：第一个元素解析成真实可执行（Windows 上 mmx→mmx.cmd），
    prompt 作为 --message 之后的参数传入；为字符串时 prompt 经 shlex 转义后拼在末尾（shell=True）。
    mmx 输出为 JSON，取 content[].text。
    无配置 / 未启用 / 调用失败 / 输出无法解析 → 返回 None（上层回退确定性抽取）。
    """
    if not _MINIMAX_CFG.exists():
        return None
    try:
        spec = json.loads(_MINIMAX_CFG.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not spec.get("enabled"):
        return None
    prompt = spec.get(
        "prompt",
        "用最多两句中文、换行分隔，总结以下今日日记的核心要点"
        "（不要列清单、不要重复待办）：\n\n{text}",
    ).format(text=diary_text)
    cmd = spec.get("command")
    if not cmd:
        return None
    try:
        if isinstance(cmd, list):
            resolved = [_resolve_exe(cmd[0])] + list(cmd[1:])
            proc = subprocess.run(
                resolved + [prompt], capture_output=True, text=True,
                timeout=spec.get("timeout", 90), encoding="utf-8")
        else:
            proc = subprocess.run(
                f"{cmd} {shlex.quote(prompt)}", shell=True, capture_output=True,
                text=True, timeout=spec.get("timeout", 90), encoding="utf-8")
        if proc.returncode == 0:
            text = _extract_text_from_mmx(proc.stdout)
            if text:
                if any(h in text for h in ("请提供", "我无法", "请告诉我",
                                            "需要你提供", "请分享", "暂时无法",
                                            "没有内容", "请补充", "请发送",
                                            "请把", "我需要先", "贴上来",
                                            "发给我", "看到日记", "尚未收到")):
                    print("[MiniMax] 返回为求助/拒绝语，回退确定性抽取",
                          file=sys.stderr)
                    return None
                return text
            print(f"[MiniMax] 输出无可用文本（raw={proc.stdout[:120]!r}）",
                  file=sys.stderr)
        else:
            print(f"[MiniMax] 调用失败 rc={proc.returncode}："
                  f"{proc.stderr[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"[MiniMax] 调用异常，回退确定性抽取：{e}", file=sys.stderr)
    return None


def run_journal_agent(vault: Optional[str] = None, day: Optional[date] = None,
                      max_n: int = 4) -> JournalPanels:
    """「agent」读当天日记并总结三块面板（仅在日记变化时调用）。

    日记内容 → MiniMax CLI 总结成 1-2 句笔记（config/minimax.json 启用时）；
    MiniMax 不可用/失败时回退到确定性抽取。Calendar Today 与 To Do 为结构化清单，
    由确定性抽取得到（MiniMax 主要服务于需要压缩成短句的 Notes）。
    """
    day = day or date.today()
    diary = _today_diary(vault, day)
    source = str(diary) if diary else ""

    diary_text = ""
    if diary is not None and diary.exists():
        try:
            diary_text = diary.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            diary_text = ""
    diary_text = diary_text[:6000]   # 截断保护：避免超长日记超出解析/参数上限

    # 搞点什么（C-02）：从当天日记「🛠️ 搞点什么」区块抽『概要』
    make_summary = _extract_make_summary(diary_text)
    if make_summary:
        print(f"[搞点什么] 已抽 {len(make_summary)} 行概要（来自日记「搞点什么」区块）")
    else:
        print("[搞点什么] 日记无「搞点什么」概要，面板留空")

    # To Do（O-04）：当天日记「待办」区（未完成项）
    todo = obsidian_collector.collect_today_todos(
        vault=vault, day=day, max_n=max_n)

    # 笔记：优先从当天日记「今日复盘」的『概要』确定性抽取（无需联网）；
    # 复盘缺概要时回退到 MiniMax 摘要兜底，避免 Notes 空白。
    notes = _extract_review_summary(diary_text)
    if notes:
        print(f"[笔记] 采用日记复盘概要（{len(notes)} 行）")
    else:
        # 复盘缺概要时的兜底：MiniMax 总结剔除待办后的叙事正文
        prose = _extract_prose(diary_text)
        summary = summarize_notes_with_minimax(prose) if prose else None
        if summary:
            notes = [ln.strip() for ln in summary.split("\n") if ln.strip()][:2]
            print("[笔记] 复盘无概要，回退 MiniMax 摘要")
        else:
            notes = []
            print("[笔记] 今日无复盘概要/叙事，Notes 留空")

    return JournalPanels(
        make_summary=make_summary,
        notes=notes,
        todo=todo,
        source=source,
        computed_at=datetime.now().isoformat(timespec="seconds"),
        from_cache=False,
    )


def get_journal_panels(vault: Optional[str] = None, day: Optional[date] = None,
                       force: bool = False, max_n: int = 4) -> JournalPanels:
    day = day or date.today()
    diary = _today_diary(vault, day)

    # 当天日记不存在：无内容可缓存，返回空（不写缓存，避免误用旧数据）
    if diary is None:
        return JournalPanels(source="", computed_at=None, from_cache=False)

    mtime = diary.stat().st_mtime

    # 命中缓存：同一文件 + mtime 未变 + 非强制重算 → 复用旧数据
    if not force and _CACHE_PATH.exists():
        try:
            cache = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            if (cache.get("schema") == 2
                    and cache.get("source") == str(diary)
                    and abs(cache.get("source_mtime", -1) - mtime) < 1e-6):
                return JournalPanels(
                    make_summary=cache.get("make_summary", []),
                    notes=cache.get("notes", []),
                    todo=cache.get("todo", []),
                    source=cache.get("source", ""),
                    computed_at=cache.get("computed_at"),
                    from_cache=True,
                )
        except (json.JSONDecodeError, OSError):
            pass  # 缓存损坏 → 落入下方重新计算

    # 未命中：唤起 agent 重读日记并总结，写入缓存
    panels = run_journal_agent(vault=vault, day=day, max_n=max_n)
    panels.source = str(diary)
    _save_cache(panels, mtime)
    return panels


def _save_cache(panels: JournalPanels, mtime: float) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps({
                "schema": _CACHE_SCHEMA,
                "source": panels.source,
                "source_mtime": mtime,
                "computed_at": panels.computed_at,
                "make_summary": panels.make_summary,
                "notes": panels.notes,
                "todo": panels.todo,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[缓存] 写入失败（不影响本次渲染）：{e}", file=sys.stderr)


if __name__ == "__main__":
    p = get_journal_panels()
    print(f"source={p.source}")
    print(f"from_cache={p.from_cache}  computed_at={p.computed_at}")
    print(f"\n[搞点什么] {p.make_summary}")
    print(f"\n[NOTES] {p.notes}")
    print(f"\n[TO DO] {p.todo}")
