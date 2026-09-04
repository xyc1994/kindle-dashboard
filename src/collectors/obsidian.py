# -*- coding: utf-8 -*-
"""
Obsidian Vault 采集器（只读，绝不修改任何笔记文件）。

从 Vault 抽取两类真实内容，填充看板的 待办 / 笔记 面板：
- 待办（TODO）：日记与备忘录里的 `- [ ]` 未完成任务（含 `1. [ ]` 编号任务）。
- 笔记（NOTES）：日记里非任务的 `- ` 实录/灵感/复盘行。

扫描范围（可在 VAULT 下任意位置，按文件名日期或文件 mtime 排序取最新）：
- 01_Daily/**/*.md        日记
- 05_Memos/Memos_Active/**/*.md  活跃备忘录（本项目当前为空，自动跳过）

设计原则：第一阶段保持 Vault 只读。任何解析失败都返回空列表，由上层回退到演示数据。
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

# 默认 Vault 根：本项目的上级「百度同步盘」即 Vault（如 E:/BaiduSyncdisk）
# 可用环境变量 OBSIDIAN_VAULT_PATH 覆盖。
_DEFAULT_VAULT = os.environ.get("OBSIDIAN_VAULT_PATH") or r"E:\BaiduSyncdisk"

_DAILY_GLOB = "01_Daily"
_MEMO_GLOB = os.path.join("05_Memos", "Memos_Active")

_TASK_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s*\[\s*\]\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(\S.*?)\s*$")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_PLACEHOLDER = ("待办事项", "____", "此处", "示例", "todo")
_META = ("synced", "frontmatter", "SOP", "AGENTS", "抽取", "wikilink", "链接")
_CJK = re.compile(r"[一-鿿]")
NOTES_MAX_AGE_DAYS = 21


def _clean(text: str) -> str:
    text = _BOLD_RE.sub(r"\1", text)        # 去掉 **粗体**
    text = text.replace("`", "").strip()
    return text


def _is_placeholder(text: str) -> bool:
    t = text.strip()
    if len(t) < 4:
        return True
    low = t.lower()
    return any(p in low for p in _PLACEHOLDER)


def _daily_date(path: Path) -> date:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime).date()


def _scan_files(vault: Path) -> List[Path]:
    files: List[Path] = []
    for sub in (_DAILY_GLOB, _MEMO_GLOB):
        d = vault / sub
        if d.exists():
            files.extend(d.rglob("*.md"))
    # 去重 + 按日期（日记文件名）降序，日记优先
    uniq = {f.resolve(): f for f in files}
    return sorted(uniq.values(), key=_daily_date, reverse=True)


def collect_todos(vault: Optional[str] = None, max_n: int = 4) -> List[str]:
    """返回清洗后的未完成待办文本列表（最新在前）。"""
    vault = Path(vault or _DEFAULT_VAULT)
    out: List[str] = []
    seen = set()
    for f in _scan_files(vault):
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for ln in lines:
            m = _TASK_RE.match(ln)
            if not m:
                continue
            txt = _clean(m.group(1))
            if not txt or _is_placeholder(txt) or txt in seen:
                continue
            seen.add(txt)
            out.append(txt)
            if len(out) >= max_n:
                return out
    return out


# 今日待办区：callout `>[!todo]` 或标题含「待办」内的任务行；兼容 callout 嵌套 `>>- [ ]`
_SECTION_TASK_RE = re.compile(r"^\s*>*\s*(?:[-*]|\d+\.)\s*\[\s*\]\s+(.+?)\s*$")
_ENTER_RE = re.compile(r"^\s*>\s*\[!todo", re.I)
_ENTER_HEAD_RE = re.compile(r"^#{1,6}\s.*待办")
_LEAVE_RE = re.compile(r"^#{1,6}\s")
_LEAVE_CALLOUT_RE = re.compile(r"^\s*>\s*\[!")


def collect_today_todos(vault: Optional[str] = None, day=None,
                        max_n: int = 4) -> List[str]:
    """返回「当天日记」里『待办』区的未完成任务（清洗后）。

    机制：每次刷新时按当天日期定位 01_Daily/<YYYY-MM-DD>.md，找到
    `>[!todo]` callout（或标题含「待办」的段落），抽取其中 `- [ ]` 任务，
    直到遇到下一个标题 / callout 为止。找不到日记或待办区则为空列表。
    """
    vault = Path(vault or _DEFAULT_VAULT)
    day = day or date.today()
    diary = vault / _DAILY_GLOB / f"{day.isoformat()}.md"
    if not diary.exists():
        return []
    try:
        lines = diary.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []

    out: List[str] = []
    seen = set()
    in_section = False
    for ln in lines:
        if in_section:
            # 离开：遇到新标题或另一个 callout（不是当前待办 callout 的内容行）
            if _LEAVE_RE.match(ln) or _LEAVE_CALLOUT_RE.match(ln):
                in_section = False
        if not in_section:
            if _ENTER_RE.match(ln) or _ENTER_HEAD_RE.match(ln):
                in_section = True
                continue
        if in_section:
            m = _SECTION_TASK_RE.match(ln)
            if not m:
                continue
            txt = _clean(m.group(1))
            if not txt or _is_placeholder(txt) or txt in seen:
                continue
            seen.add(txt)
            out.append(txt)
            if len(out) >= max_n:
                break
    return out


def collect_notes(vault: Optional[str] = None, max_n: int = 3) -> List[str]:
    """返回日记里近期的非任务实录/灵感行（最新在前）。

    质量门槛：必须是含中文的实质性句子（非 wikilink、非元数据），
    且仅取最近 NOTES_MAX_AGE_DAYS 天内的日记，避免展示陈旧内容。
    """
    vault = Path(vault or _DEFAULT_VAULT)
    today = date.today()
    out: List[str] = []
    seen = set()
    for f in _scan_files(vault):
        d = _daily_date(f)
        if (today - d).days > NOTES_MAX_AGE_DAYS:
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for ln in lines:
            m = _BULLET_RE.match(ln)
            if not m:
                continue
            txt = _clean(m.group(1))
            if not txt or _is_placeholder(txt) or txt in seen:
                continue
            if "[" in txt or "]" in txt:          # 跳过 wikilink
                continue
            low = txt.lower()
            if any(k in low for k in _META):       # 跳过元数据行
                continue
            if not _CJK.search(txt) or len(txt) < 8:
                continue
            seen.add(txt)
            out.append(txt)
            if len(out) >= max_n:
                return out
    return out


def _due_and_overdue(text: str):
    """从待办文本里抽截止日期；若有过去日期则标记逾期。"""
    m = _DATE_RE.search(text)
    if not m:
        return "", False
    try:
        d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return "", False
    due = m.group(1)
    return due, d < date.today()


if __name__ == "__main__":
    print("VAULT:", _DEFAULT_VAULT)
    todos = collect_todos()
    print(f"\n[待办] 共 {len(todos)} 条（最多 4）：")
    for t in todos:
        due, od = _due_and_overdue(t)
        flag = " [逾期]" if od else ""
        print(f"  - {t}{('  @'+due) if due else ''}{flag}")
    notes = collect_notes()
    print(f"\n[笔记] 共 {len(notes)} 条（最多 3）：")
    for n in notes:
        print(f"  - {n}")
