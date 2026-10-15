# coding: utf-8
"""
从「用户纠正过的文本」里学词条。

对标 Typeless 的 Dictionary 自动学习：*"When you correct a word after dictating,
Typeless automatically recognizes your preferred spelling and adds the word."*

我们有两个入口，都落到这里：

  1. **「记录」页的「✏️ 纠正」** —— 用户对着识别结果改一遍，我们 diff 出改动的词，
     问一句要不要收进词典。**你改了什么我们才知道什么**，看得见、可回退。
  2. **上屏之后的自动回读**（`core/client/correction_learner.py`）—— 走系统无障碍
     接口（Windows UI Automation）回读刚才那个输入框，自动发现你改了什么。
     这是 Typeless 的做法，**只在本地读、本地比对**，可以在设置里关掉。

落到 hot.txt 的形态是 `正确写法 | 常见误写` —— 比 Typeless 那种"只记正确写法"
更强：热词是用**音素**匹配别名的，所以记下"你实际会念错成什么"之后，
下次说错也照样能改对。
"""

from __future__ import annotations

import re
from typing import List, Tuple

# 切成"词"：连续的英文字母/数字算一个词，每个汉字算一个词，其余（标点空白）单独成 token
_TOKEN_RE = re.compile(r'[A-Za-z0-9_\-\']+')

TYPICAL_MIN_LEN = 2          # 太短的改动不当词条（多半是标点或单字口癖）


def tokenize(text: str) -> List[str]:
    """把文本切成可比对的 token（英文按词、中文按字、标点单独）。"""
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        m = _TOKEN_RE.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
        else:
            out.append(text[i])
            i += 1
    return out


def _is_wordy(token: str) -> bool:
    """是不是"实义"token（汉字或字母数字），标点空白不算。"""
    return bool(re.search(r'[\w一-鿿]', token))


def _widen(a: List[str], b: List[str], i1: int, i2: int, j1: int, j2: int,
           min_len: int) -> Tuple[int, int, int, int]:
    """
    改动片段太短时，向两侧扩到两侧都至少有 min_len 个实义 token。

    中文是逐字比对的，只改一个字（`老汪` → `老王`）diff 出来就是单个「汪 → 王」，
    单个字进词典没有意义。往前扩一位就得到「老汪 → 老王」——这正是用户想要的词。

    判据是**字符数**不是 token 数：英文一个 token 就是一个完整的词（`wirld`），
    按 token 数会把它误判成"太短"然后一路扩成 `the wirld`。

    扩不出更多（到句首/句尾、或两侧对不齐）就停，交给用户在纠正对话框里改。
    """
    while True:
        w_len = sum(len(t) for t in a[i1:i2] if _is_wordy(t))
        r_len = sum(len(t) for t in b[j1:j2] if _is_wordy(t))
        if min(w_len, r_len) >= min_len:
            return i1, i2, j1, j2
        if (i1 > 0 and j1 > 0
                and _is_wordy(a[i1 - 1]) and _is_wordy(b[j1 - 1])):
            i1 -= 1
            j1 -= 1
            continue
        if (i2 < len(a) and j2 < len(b)
                and _is_wordy(a[i2]) and _is_wordy(b[j2])):
            i2 += 1
            j2 += 1
            continue
        return i1, i2, j1, j2


def diff_words(old: str, new: str, min_len: int = TYPICAL_MIN_LEN) -> List[Tuple[str, str]]:
    """
    比对纠正前后的文本，返回 [(误写, 正确写法), ...]。

    中文逐字比对、英文按词比对。只改一两个字的片段会向两侧扩到至少
    `min_len` 个实义字（单个字的词条没有意义）。
    纯标点改动、纯增删都不产生词条 —— 那些不是"这个词被念错了"。

    更长的专有名词（比如 diff 只认出「宇树 → 语数」、而用户其实想记「宇树科技」）
    交给纠正对话框里手动改 —— 我们不去猜用户心里的词边界。
    """
    import difflib

    a, b = tokenize(old or ''), tokenize(new or '')
    pairs: List[Tuple[str, str]] = []
    seen = set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, a, b, autojunk=False).get_opcodes():
        if tag != 'replace':
            continue
        i1, i2, j1, j2 = _widen(a, b, i1, i2, j1, j2, min_len)
        wrong = ''.join(a[i1:i2]).strip()
        right = ''.join(b[j1:j2]).strip()
        if not wrong or not right or wrong == right:
            continue
        # 纯标点/空白改动不算词条
        if not _is_wordy(wrong) or not _is_wordy(right):
            continue
        if sum(len(t) for t in b[j1:j2] if _is_wordy(t)) < min_len:
            continue
        if (wrong, right) in seen:
            continue
        seen.add((wrong, right))
        pairs.append((wrong, right))
    return pairs


def to_dictionary_lines(pairs: List[Tuple[str, str]]) -> List[str]:
    """把改动对拼成 hot.txt 的行（`正确写法 | 误写`，误写可能多个就并进别名）。"""
    merged: dict = {}
    for wrong, right in pairs:
        merged.setdefault(right, [])
        if wrong not in merged[right]:
            merged[right].append(wrong)
    return [f'{right} | ' + ' | '.join(merged[right]) for right in merged]


def merge_into_hot(text: str, pairs: List[Tuple[str, str]]) -> Tuple[str, List[str]]:
    """
    把改动对并进 hot.txt 文本：已有同名词条就补别名，没有就新增一行。

    返回 (新文本, 实际做了什么的人话说明)。
    """
    from core.client.hotword.hot_file import parse_words, format_word

    lines = (text or '').splitlines()
    existing = {w.target: w for w in parse_words(text)}
    added: List[str] = []
    merged_alias: List[str] = []
    next_line_no = len(lines) + 1

    for wrong, right in dict.fromkeys(pairs):        # 去重且保持顺序
        w = existing.get(right)
        if w is None:
            lines.append(format_word(right, [wrong]))
            added.append(f'{right}（别名 {wrong}）')
            # 记进 existing，否则同一对改动出现两次会被追加两行
            existing[right] = _make_word(next_line_no, right, (wrong,))
            next_line_no += 1
            continue
        if wrong in w.forms:
            continue
        parts = [w.target, *(w.aliases or ()), wrong]
        lines[w.line_no - 1] = format_word(w.target, parts[1:], w.blocked)
        merged_alias.append(f'{w.target} + 别名 {wrong}')
        # 同一行被改过就更新记录，避免二次改动写回旧行
        existing[right] = _make_word(w.line_no, w.target, tuple(parts[1:]), w.blocked)

    new_text = '\n'.join(lines)
    if (text or '').endswith('\n') or not new_text.endswith('\n'):
        new_text += '\n'
    return new_text, added + merged_alias


def _make_word(line_no: int, target: str, aliases: tuple = (), blocked: tuple = ()):
    """构造一个 hot_file.Word（用于在内存里跟踪刚写入的那一行）。"""
    from core.client.hotword.hot_file import Word
    return Word(line_no=line_no, target=target, aliases=aliases, blocked=blocked)


def save_pairs(pairs: List[Tuple[str, str]]) -> dict:
    """
    把改动对并进 hot.txt 并落盘，返回结构化结果：

        {'changed': bool,          # 有没有真的写进去（重复的改动会是 False）
         'what': [人话说明, ...],
         'message': 给用户看的一句话}

    自动学词（core/client/correction_learner.py）用这个版本来判断"要不要弹提示" ——
    没改动还弹"学到了"会让人以为它在瞎学。
    """
    from core.client.hotword import hot_file

    current = hot_file.read_hot()
    if current is None:
        return {'changed': False, 'what': [], 'message': '读不到 hot.txt，没能保存。'}
    new_text, what = merge_into_hot(current, pairs)
    if not what:
        return {'changed': False, 'what': [],
                'message': '这些词已经在词典里了，没有改动。'}
    if not hot_file.write_hot(new_text):
        return {'changed': False, 'what': [], 'message': '写入 hot.txt 失败，详见日志。'}
    return {
        'changed': True,
        'what': what,
        'message': ('已加进词典：\n\n  ' + '\n  '.join(what) +
                    '\n\n热词会在几秒内自动生效（不用重启）。'),
    }


def learn_and_save(pairs: List[Tuple[str, str]]) -> str:
    """
    把改动对并进 hot.txt 并落盘，返回给用户看的一句话。

    写盘走 `hot_file.write_hot`（原子写 + 备份）；热词管理器那边有 3 秒防抖的
    文件监视器，会自动重载 —— 所以这里不需要再通知谁。

    本函数只碰文件，不需要 app 对象，因此在任何界面里都能直接用。
    """
    return save_pairs(pairs)['message']
