# coding: utf-8
"""
hot.txt 的读 / 写 / 体检

给设置面板的「热词」页用。这一份**故意做得很薄** —— 热词表现在完全由用户自己
维护，我们只负责把文件读出来显示、把用户改好的原文原样写回去，不做任何解析。

（之前还有一份「自动学习」的热词表，由大模型在识别过程中发现并写入。
 实测下来它的收益很小 —— 公共名词好的提示词本来就能改对（5/5 稳定、13/13 命中），
 而它的风险很实：大模型偶尔会把你写对的专有名词"改"成它更眼熟的同音词
 （「松鼠云」→「松树云」，8 次里 2 次），这个词对一旦被记进强制替换通道就
 **永久生效且不报错**。本机音素模型也分辨不了这种修正的好赖（好词对坏词对
 算出来都是 1.000）。所以整个自动学习连同 `[[SM-FIX]]` 那一套一起删掉了。）

## 关于 lint()

热词表有个静默的坑：**一个词完整包含另一个词**。比如写

    宇树科技 | 宇树

会因为音素 RAG 是"模糊子串匹配"，让用户每说一次"宇树"都被补成"宇树科技"。
文件本身看不出问题，纠错也不报错，只是结果悄悄变了。
所以这里做一次很轻的体检，把可疑行报给界面，让用户自己决定。
"""

from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

# 项目根（打包时 = exe 同级目录）。别用 __file__ 上溯：打包时 core/ 可能是
# 目录联接，resolve() 会展开成源码树路径 —— 那样在打包版里编辑热词表，
# 改的是源码树的 hot.txt，发行版自己的那份纹丝不动。
from core.tools.paths import install_root as _install_root
_ROOT = _install_root()

_LOCK = threading.RLock()


def hot_path(base: Optional[Path] = None) -> Path:
    """
    定位 hot.txt。

    `base` 一般传 `HotwordManager.files['hot']` —— 它可能是相对路径
    （默认就是 `Path('hot.txt')`），这里统一按**项目根**解析，而不是 cwd，
    免得进程工作目录被改掉时"写成一处、读另一处"。
    """
    if base is None:
        return _ROOT / 'hot.txt'
    p = Path(base)
    return p if p.is_absolute() else _ROOT / p


def read_hot(base: Optional[Path] = None) -> Optional[str]:
    """
    读原文。**读不到返回 None**（而不是空串）—— 界面靠这个区分
    "文件读不出来"和"文件是空的"，前者不能建编辑框，
    否则用户一保存就把原来的内容覆盖成空的了。
    """
    target = hot_path(base)
    try:
        if not target.is_file():
            return None
        return target.read_text(encoding='utf-8')
    except Exception:
        return None


def write_hot(text: str, base: Optional[Path] = None) -> bool:
    """
    原子写 hot.txt（临时文件 -> fsync -> 备份 .bak -> os.replace）。

    **内容没变就不写盘** —— 这个文件有 watchdog 盯着，每次无谓的重写都会
    触发一次全量重载（重建音素索引），而设置面板每次保存都会调到这里。
    """
    if not isinstance(text, str):
        return False
    target = hot_path(base)

    with _LOCK:
        try:
            try:
                if target.is_file() and target.read_text(encoding='utf-8') == text:
                    return True
            except Exception:
                pass

            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + '.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            if target.is_file():
                try:
                    shutil.copy2(target, target.with_name(target.name + '.bak'))
                except Exception:
                    pass
            os.replace(tmp, target)
            return True
        except Exception:
            return False


def lint(text: str) -> List[str]:
    """
    体检热词表，返回给用户看的警告（空列表 = 没问题）。

    只查一件事：**一个词完整包含另一个词**。这是唯一会静默改变结果、
    而文件本身完全看不出问题的坑。两个方向都有害：

        宇树科技 | 宇树          说「宇树」时被补成「宇树科技」（被加长）
        云深处 | 云深处科技      说「云深处科技」时被截短成「云深处」

    ⚠️ 注意**不能**跳过目标词自己 —— 第二个例子里的危险恰恰是
    "目标词被包含在别名里"，一开始漏了这条（`other is target: continue`），
    测试直接抓出来了。
    """
    warnings: List[str] = []
    if not text:
        return warnings

    rows = []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.split('~~~')[0].strip()
        if not line or line.startswith('#'):
            continue
        parts = [p.strip() for p in line.split('|') if p.strip()]
        if parts:
            rows.append((i, parts))

    all_words = [w for _, ps in rows for w in ps]

    for lineno, parts in rows:
        target = parts[0]
        for alias in parts:
            for other in all_words:
                if other == alias or len(other) < 2:
                    continue
                if other not in alias:
                    continue
                if other == target:
                    msg = (f'第 {lineno} 行：「{alias}」里包含了「{target}」，'
                           f'说完整的「{alias}」会被截短成「{target}」。'
                           f'建议把「{alias}」单独写成一行。')
                else:
                    msg = (f'第 {lineno} 行：「{other}」完整包含在「{alias}」里，'
                           f'说「{other}」时会被换成「{target}」。'
                           f'建议删掉多余的别名，或给「{other}」单独建一行。')
                if msg not in warnings:
                    warnings.append(msg)
                break
    return warnings[:5]


# ============================================================
#  词条级增删改（设置面板「词典」页用）
#
#  hot.txt 的行格式：
#      target | 别名1 | 别名2 ~~~ 黑名单1 | 黑名单2
#  以 # 开头的整行是注释，空行忽略。
# ============================================================

@dataclass
class Word:
    """hot.txt 里的一条词条。"""
    line_no: int
    target: str                            # 正确写法（行首那一个）
    aliases: Tuple[str, ...] = ()
    blocked: Tuple[str, ...] = ()          # ~~~ 后面的黑名单
    raw: str = ''

    @property
    def forms(self) -> Tuple[str, ...]:
        """这一条能匹配的所有写法（正确写法 + 别名）。"""
        return (self.target,) + tuple(self.aliases)


def parse_words(text: str) -> List[Word]:
    """解析成词条列表（注释行、空行跳过）；line_no 是 1-based 行号。"""
    out: List[Word] = []
    for i, raw in enumerate((text or '').splitlines(), 1):
        head, _, tail = raw.partition('~~~')
        line = head.strip()
        if not line or line.startswith('#'):
            continue
        parts = [p.strip() for p in line.split('|') if p.strip()]
        if not parts:
            continue
        blocked = tuple(p.strip() for p in tail.split('|') if p.strip())
        out.append(Word(line_no=i, target=parts[0], aliases=tuple(parts[1:]),
                        blocked=blocked, raw=raw))
    return out


def format_word(target: str, aliases: Iterable[str] = (),
                blocked: Iterable[str] = ()) -> str:
    """拼成 hot.txt 的一行。"""
    parts = [target.strip()] + [a.strip() for a in aliases if a and a.strip()]
    line = ' | '.join(parts)
    bl = [b.strip() for b in blocked if b and b.strip()]
    if bl:
        line += ' ~~~ ' + ' | '.join(bl)
    return line


def find_word(text: str, target: str) -> Optional[Word]:
    """按正确写法找词条（大小写敏感 —— 热词表本来就区分大小写）。"""
    t = (target or '').strip()
    if not t:
        return None
    for w in parse_words(text):
        if w.target == t:
            return w
    return None


def add_word(text: str, target: str, aliases: Iterable[str] = (),
             blocked: Iterable[str] = ()) -> Tuple[str, bool]:
    """
    追加一条词条到文件末尾。已存在同名 target 时不重复添加。

    返回 (新文本, 是否真的加了)。
    """
    target = (target or '').strip()
    if not target or find_word(text, target):
        return text, False
    new_line = format_word(target, aliases, blocked)
    body = (text or '').rstrip('\n')
    return (f'{body}\n{new_line}\n' if body else f'{new_line}\n'), True


def remove_word(text: str, line_no: int) -> str:
    """删掉某一行的词条（只删这一行，注释与其它词条不动）。"""
    lines = (text or '').splitlines()
    if not (1 <= line_no <= len(lines)):
        return text
    del lines[line_no - 1]
    return '\n'.join(lines) + ('\n' if (text or '').endswith('\n') else '')


def import_csv(text: str, csv_text: str) -> Tuple[str, int, List[str]]:
    """
    从 CSV 文本导入词条。

    每行一列就是词条；多列时**第一列是正确写法、其余列是别名**
    （正好对应 hot.txt 的 `A | B | C`）。`#` 开头的行当注释跳过。

    返回 (新文本, 新增条数, 已存在而跳过的词条列表)。
    """
    added = 0
    skipped: List[str] = []
    for raw in (csv_text or '').splitlines():
        line = raw.strip().lstrip('﻿')
        if not line or line.startswith('#'):
            continue
        cells = [c.strip().strip('"').strip("'") for c in line.split(',')]
        cells = [c for c in cells if c]
        if not cells:
            continue
        text, ok = add_word(text, cells[0], cells[1:])
        if ok:
            added += 1
        else:
            skipped.append(cells[0])
    return text, added, skipped
