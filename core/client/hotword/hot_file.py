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
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# 项目根：core/client/hotword/hot_file.py -> 上溯四级
_ROOT = Path(__file__).resolve().parents[3]

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
