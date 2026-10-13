# coding: utf-8
"""
识别历史：把日记归档解析成结构化记录，供设置面板的「记录」页浏览与操作。

**不新建数据格式** —— 数据源就是已有的日记归档（`core/client/diary/diary_writer.py` 写的），
两种文件：

  `年份/月份/日期.md`        每次听写的文本 + 音频链接
      格式：`[HH:MM:SS](相对音频路径) 识别文本`，没有音频时是 `HH:MM:SS 识别文本`
  `年份/月份/日期-角色名.md`  与 LLM 的对话记录
      格式：`#### [[HH:MM:SS]]((音频)` + `**输入**：…` + `**输出**：…`

这两种正好对应「听写」与「对话」两类，所以「记录」页可以像 Typeless 那样按类型筛选。

设计约束：
  - 只读解析 + 精确删除（按行区间），不动日记的写入逻辑
  - 删除是**真的删**（用户明确点了删除才调），且只动被选中的那一条
  - 本模块不依赖 Tk，可以单独测试
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from core.tools.paths import install_root

# 听写条目：[14:25:04](相对路径) 文本   或者无音频：14:25:04 文本
#
# 注意：音频文件名里**本身带括号**（形如 `(20261012-142504)那个，咱咱问.wav`），
# 所以不能写成 `[^)]+` —— 那样会在第一个右括号处截断。改成"一路匹配到媒体扩展名 + 右括号"。
_DICTATION_RE = re.compile(
    r'^\[?(?P<hms>\d{2}:\d{2}:\d{2})\]?'
    r'(?:\((?P<audio>.+\.(?:wav|mp3|m4a|flac|ogg|aac))\))?'
    r'[\s　]*(?P<text>.*)$',
    re.IGNORECASE,
)
# 对话条目的时间行：#### [[14:40:53]](((20261012-144053)xxx.wav)
_CHAT_HEAD_RE = re.compile(
    r'^####\s*\[\[(?P<hms>\d{2}:\d{2}:\d{2})\]\]\(\('
    r'\(?(?P<audio>.+\.(?:wav|mp3|m4a|flac|ogg|aac))\)?',
    re.IGNORECASE,
)
_IN_RE = re.compile(r'^\*\*输入\*\*：\s*(?P<v>.*)$')
_OUT_RE = re.compile(r'^\*\*输出\*\*：\s*(?P<v>.*)$')

# 日记文件名：13.md / 13-默认.md
_DAY_FILE_RE = re.compile(r'^(?P<day>\d{2})(?:-(?P<role>.+))?\.md$')
_MONTH_DIR_RE = re.compile(r'^\d{2}$')
_YEAR_DIR_RE = re.compile(r'^\d{4}$')

KIND_DICTATION = 'dictation'
KIND_CHAT = 'chat'


@dataclass
class Entry:
    """一条记录。"""
    kind: str                       # 'dictation' | 'chat'
    when: datetime
    text: str                       # 听写文本 / 对话的「输入」
    reply: str = ''                 # 对话的「输出」（听写为空）
    audio: Optional[Path] = None    # 绝对路径，可能不存在
    role: str = ''                  # 对话的角色名（听写为空）
    md_file: Path = field(default_factory=Path)
    line_start: int = 0             # 1-based，含
    line_end: int = 0               # 1-based，含

    @property
    def time_text(self) -> str:
        return self.when.strftime('%m-%d %H:%M:%S')

    @property
    def audio_exists(self) -> bool:
        return self.audio is not None and self.audio.exists()

    def matches(self, keyword: str) -> bool:
        if not keyword:
            return True
        kw = keyword.lower()
        return kw in self.text.lower() or kw in self.reply.lower()


# ============================================================
#  定位与遍历
# ============================================================

def diary_root() -> Path:
    """日记归档的根目录（安装目录下的 `年份/月份/`）。"""
    return install_root()


def iter_md_files(limit_months: int = 6) -> List[Path]:
    """
    列出日记 md 文件，**新的在前**。

    目录结构固定是 `年份/月份/日期.md`，所以按名字倒序排就是时间倒序，
    不需要读文件内容。limit_months 限制往回找多少个月（默认半年，够用且快）。
    """
    root = diary_root()
    files: List[Path] = []
    years = sorted((d for d in root.iterdir() if d.is_dir() and _YEAR_DIR_RE.match(d.name)),
                   key=lambda p: p.name, reverse=True)
    for year in years:
        months = sorted((d for d in year.iterdir() if d.is_dir() and _MONTH_DIR_RE.match(d.name)),
                        key=lambda p: p.name, reverse=True)
        for month in months[:limit_months]:
            for f in month.glob('*.md'):
                if _DAY_FILE_RE.match(f.name):
                    files.append(f)
        if len(files) >= limit_months * 31:
            break
    return sorted(files, reverse=True)


# ============================================================
#  解析
# ============================================================

def _resolve_audio(md_file: Path, rel: str) -> Optional[Path]:
    """
    音频路径有三种形态（取决于写日记时音频在不在 md 同级目录）：

      `assets/xxx.wav`              相对**这个 md 文件**
      `2026/10/assets/xxx.wav`      相对**安装根目录**（diary_writer 的兜底分支写出来的）
      `xxx.wav`                     只有文件名（对话记录的归档就是这么写的）

    按顺序试几个基准，返回第一个存在的；都不存在就返回相对 md 的那个（界面会显示"无音频"）。
    """
    if not rel:
        return None
    rel = rel.strip().replace('%20', ' ')
    by_md: Optional[Path] = None
    candidates = [md_file.parent / rel]
    if '/' not in rel and '\\' not in rel:      # 只有文件名 → 多半在 assets 里
        candidates.insert(0, md_file.parent / 'assets' / rel)
    candidates.append(diary_root() / rel)
    for cand in candidates:
        try:
            p = cand.resolve()
        except Exception:
            continue
        if by_md is None:
            by_md = p
        if p.exists():
            return p
    return by_md


def parse_file(md_file: Path) -> List[Entry]:
    """解析一个日记 md，返回其中的记录（按文件里的顺序）。"""
    try:
        lines = md_file.read_text(encoding='utf-8', errors='replace').splitlines()
    except Exception:
        return []

    m = _DAY_FILE_RE.match(md_file.name)
    if not m:
        return []
    day = int(m.group('day'))
    role = (m.group('role') or '').strip()
    try:
        base = datetime(md_file.parent.parent.name and int(md_file.parent.parent.name) or 1970,
                        int(md_file.parent.name), day)
    except Exception:
        base = datetime(1970, 1, 1)

    entries: List[Entry] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        # ---- 对话条目 ----
        head = _CHAT_HEAD_RE.match(line.strip())
        if head:
            hms = head.group('hms')
            audio = _resolve_audio(md_file, head.group('audio') or '')
            text = reply = ''
            j = i + 1
            while j < n and not _CHAT_HEAD_RE.match(lines[j].strip()):
                mm = _IN_RE.match(lines[j].strip())
                if mm:
                    text = mm.group('v')
                mm = _OUT_RE.match(lines[j].strip())
                if mm:
                    reply = mm.group('v')
                j += 1
            entries.append(Entry(
                kind=KIND_CHAT, when=_at(base, hms), text=text, reply=reply,
                audio=audio, role=role, md_file=md_file,
                line_start=i + 1, line_end=j))
            i = j
            continue

        # ---- 听写条目 ----
        stripped = line.strip()
        if stripped and not stripped.startswith(('#', '```', '**', '>')):
            dm = _DICTATION_RE.match(stripped)
            if dm and dm.group('text'):
                entries.append(Entry(
                    kind=KIND_DICTATION, when=_at(base, dm.group('hms')),
                    text=dm.group('text').strip(),
                    audio=_resolve_audio(md_file, dm.group('audio') or ''),
                    md_file=md_file, line_start=i + 1, line_end=i + 1))
        i += 1
    return entries


def _at(base: datetime, hms: str) -> datetime:
    try:
        h, mi, s = (int(x) for x in hms.split(':'))
        return base.replace(hour=h, minute=mi, second=s)
    except Exception:
        return base


def load(limit: int = 500, kind: str = '', keyword: str = '',
         limit_months: int = 6) -> List[Entry]:
    """读全部记录，按时间**倒序**。kind 传 'dictation'/'chat' 可筛选。"""
    out: List[Entry] = []
    for md in iter_md_files(limit_months):
        out.extend(parse_file(md))
        if len(out) > limit * 3:        # 多读一些再排，避免漏掉同一天里的顺序
            break
    out.sort(key=lambda e: e.when, reverse=True)
    if kind:
        out = [e for e in out if e.kind == kind]
    if keyword:
        out = [e for e in out if e.matches(keyword)]
    return out[:limit]


# ============================================================
#  删除与保留策略
# ============================================================

def delete(entry: Entry, remove_audio: bool = True) -> bool:
    """
    删掉一条记录（按行区间精确删除，**只动这一条**）。

    同时删掉它的音频文件（audio 在 assets 里、且只被这一条引用）。
    返回是否成功。
    """
    try:
        lines = entry.md_file.read_text(encoding='utf-8', errors='replace').splitlines()
        start, end = entry.line_start - 1, entry.line_end
        if not (0 <= start < len(lines)):
            return False
        # 连带吃掉紧随其后的空行，免得多删一次留下空洞
        while end < len(lines) and not lines[end].strip():
            end += 1
        del lines[start:end]
        entry.md_file.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    except Exception:
        return False

    if remove_audio and entry.audio is not None:
        try:
            if entry.audio.exists() and entry.audio.suffix.lower() in (
                    '.wav', '.mp3', '.m4a', '.flac'):
                entry.audio.unlink()
        except Exception:
            pass
    return True


def purge(keep: str) -> Tuple[int, int]:
    """
    按保留策略清理旧记录。

    keep: 'forever' | '365' | '30' | '7' | '1' | 'never'（天数的字符串，或 forever/never）
    返回 (删除的条数, 处理的天数)。
    """
    if keep in ('forever', '', None):
        return (0, 0)

    if keep == 'never':
        days = 0
    else:
        try:
            days = max(0, int(keep))
        except Exception:
            return (0, 0)

    cutoff = datetime.now() if days == 0 else datetime.now() - timedelta(days=days)
    removed = 0
    touched_days = set()
    for md in iter_md_files(limit_months=120):
        entries = [e for e in parse_file(md) if e.when < cutoff]
        if not entries:
            continue
        touched_days.add(md)
        # 从后往前删，避免行号错位
        for e in sorted(entries, key=lambda x: x.line_start, reverse=True):
            if delete(e):
                removed += 1
    return (removed, len(touched_days))


def count_by_kind(entries: Iterable[Entry]) -> Tuple[int, int]:
    """返回 (听写条数, 对话条数)。"""
    d = c = 0
    for e in entries:
        if e.kind == KIND_DICTATION:
            d += 1
        else:
            c += 1
    return (d, c)
