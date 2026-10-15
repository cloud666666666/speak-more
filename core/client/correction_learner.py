# coding: utf-8
"""
从「你上屏之后的改动」里自动学词 —— 对标 Typeless 的 Auto-added。

Typeless / Wispr Flow 都会这么干：把识别结果打到光标处，过一会儿回去看一眼那个
输入框，发现你把某个词改了，就把改动记进词典。它们靠的是系统无障碍接口
（macOS AXUIElement / Windows UI Automation）。这个模块走同一条路。

流程（**全程本地，不联网**）：
  1. 每次把识别结果打上屏，记下"我说出去的那句话 + 当时焦点在哪个控件"
  2. **盯着那个输入框看一小会儿**（默认最多 20 秒，前 8 秒每 0.25 秒采样一次）
  3. 内容一变（你把某个词改了）→ 立刻和原话 diff，像回事的改动就进词典
  4. **输入框空了** → 你发送了（或全删了），用最后看到的那份内容结算收工

为什么要连续采样而不是"隔几秒读一次"：用户常常**改完立刻回车发出去**，
输入框当场空了 —— 隔几秒再读只能读到空白，改动永远学不到（实测丢过）。
采样很便宜：读一次焦点控件是毫秒级的，占的是监视线程自己的时间。

误学是怎么防的（四道闸）：
  - **还是那个控件吗**：焦点跑到别的窗口就跳过这次采样（连着跑掉好几次才放弃）
  - **我那句话还在里面吗**：跟你现在输入框里的内容做整体比对，
    我们的原话必须基本原样还在（≥70% 的字对得上），否则说明你已经在写别的了
  - **改动幅度**：删掉重写不算"这个词被听错了"，只认小幅替换；
    而且只认 `replace`（替换），你**补字、续写、删句**都不会产生词条
  - **读音对得上吗**（最要紧的一道）：中文比音素（忽略声调）、英文只认错
    一两个字母。这条挡的是"你把半句话重写了" —— difflib 会把那种改动拆成
    好几个短替换，长度和条数都拦不住，但读音对不上（`周三→周五`）。

出错的代价很小：词条写进 hot.txt 后可以在设置面板「热词」页逐条删掉。
开关见 config_client.py 的 `learn_from_corrections`。
"""

from __future__ import annotations

import difflib
import threading
import time
from typing import Any, Dict, List, Tuple

from config_client import ClientConfig as Config
from core.client.hotword.learn import diff_words, save_pairs
from core.ui import logger

# 一次最多学几个词条。真正"听错了一个词"的改动就一处，三处已经很不寻常了；
# 再多基本是"你把这句话重写了" —— 那种改动即使每一段读音都碰巧对得上，
# 存进去也是垃圾词条。
MAX_PAIRS = 3

# 单个词条最长多少个字。**这是自动学词特有的闸**：手动「✏️ 纠正」那边是你
# 亲眼看过、点过确认的，多长都行；自动这边没人把关，跨度太长的改动多半不是
# "某个词听错了"，而是"你把半句话重写了"（或者续写的第二句恰好和第一句对齐
# 上了）—— 那种东西存进词典没有意义，还会在以后误伤正常的句子。
MAX_PAIR_CHARS = 8

# 同时在等的监视器上限（连着说好几句时别无限堆线程）
_MAX_WATCHERS = 4
_watchers = 0
_lock = threading.Lock()

# ---- 观察节奏 ----
#
# 为什么是"盯着看一会儿"而不是"隔几秒读一次"：用户常常**改完立刻回车发送**，
# 那个输入框当场就空了 —— 隔几秒再去读，读到的是一片空白，改动永远学不到。
# 所以要连续采样：一有变化就学，输入框空了就用最后看到的内容结算。
POLL_FAST_S = 0.25        # 快速阶段的采样间隔（0.25 秒，抓"改完就发送"）
POLL_FAST_UNTIL = 8.0     # 前多少秒算快速阶段
POLL_SLOW_S = 1.0         # 之后放慢（用户多半在发呆或已经发送了）
WATCH_WINDOW_S = 20.0     # 总观察时长上限，也是这个线程的最长存活时间
AWAY_LIMIT = 6            # 焦点连续这么多次不在同一个控件上就放弃
                          # （跑掉一两次没关系：切走看一眼再回来照样算）

# UIA 用不了这件事只说一次，别每句都往日志里写
_warned = False


def enabled() -> bool:
    """从纠正里学词的总开关（设置面板「热词」页）。"""
    return bool(getattr(Config, 'learn_from_corrections', True))


def watch(text: str) -> None:
    """
    记下刚上屏的这段文本，然后**盯着那个输入框看一会儿**，一有改动就学。

    从识别回调里调，**立刻返回**（真正的活儿在后台线程里做），
    所以不会给上屏这条主链路增加任何延迟。
    """
    global _watchers
    if not enabled():
        return
    text = (text or '').strip()
    # 太短的一句话 diff 不出有意义的词条（而且容易把"又说了半句"误判成改动）
    if len(text) < 4:
        return
    with _lock:
        if _watchers >= _MAX_WATCHERS:
            logger.debug('学词监视器已满，这一句跳过')
            return
        _watchers += 1
    try:
        threading.Thread(target=_watch, args=(text,), daemon=True,
                         name='learn-watch').start()
    except Exception as e:                       # pragma: no cover
        with _lock:
            _watchers -= 1
        logger.debug(f'学词监视线程起不来: {e}')


def _watch(text: str) -> None:
    """后台线程：等一会儿，回读焦点输入框，从改动里学词。"""
    global _watchers, _warned
    try:
        _watch_inner(text)
    except Exception as e:                       # 学不到词不是错误，不影响任何功能
        logger.debug(f'学词监视失败（不影响使用）: {e}')
    finally:
        with _lock:
            _watchers -= 1


def _watch_inner(text: str) -> None:
    global _warned
    try:
        from core.tools import ui_reader
    except Exception as e:
        logger.debug(f'无障碍读取模块不可用: {e}')
        return
    if not ui_reader.available():
        if not _warned:
            _warned = True
            logger.warning('学词需要 comtypes + UI Automation，当前不可用；'
                           '「从我的纠正里学词」暂时无效（其他功能不受影响）')
        return

    # T+0：记住当时正在哪儿打字。回读时如果焦点已经换了地方，就别拿别人的
    # 内容当"你的纠正"（用户常常打完字就切走）
    before = ui_reader.focused_info()
    window = float(getattr(Config, 'learn_from_corrections_window',
                           WATCH_WINDOW_S))
    deadline = time.monotonic() + max(1.0, window)

    last_seen: Optional[str] = None       # 最近一次"还认得出是我们那句话"的内容
    last_learned: Optional[str] = None    # 已经拿它学过的内容（同一份不用反复 diff）
    away = 0                              # 焦点连续不在同一个控件上的次数

    while time.monotonic() < deadline:
        info = ui_reader.focused_info()
        if not _same_control(before, info):
            away += 1
            if away >= AWAY_LIMIT:
                logger.debug(f'学词：焦点已经离开 {before.get("class")}，结束观察')
                break
            time.sleep(_poll_interval(deadline, window))
            continue
        away = 0

        current = ui_reader.read_focused_text()
        if current is None:
            logger.debug('学词：读不到当前输入框的内容（控件不支持），结束观察')
            break

        if not current.strip():
            # 输入框空了：发送出去了 / 全删了。用**最后一次看到的内容**来学 ——
            # 那正是他按下回车之前的样子，修正都在里面。
            if last_seen is not None:
                logger.debug('学词：输入框空了（多半是发送了），用最后看到的内容结算')
                _learn(text, last_seen)
            else:
                logger.debug('学词：输入框一直空着，没有可学的内容')
            break

        if _still_ours(text, current):
            last_seen = current
            if current != last_learned:
                last_learned = current
                _learn(text, current)      # 一有变化就学，不等最后一次
        # else：还不是我们那句话（字可能都还没打完），或者用户写了别的 —— 继续看

        time.sleep(_poll_interval(deadline, window))


def _poll_interval(deadline: float, window: float) -> float:
    """采样间隔：刚开始密一点（要抓住"改完就发送"），后面放慢。"""
    spent = window - max(0.0, deadline - time.monotonic())
    return POLL_FAST_S if spent < POLL_FAST_UNTIL else POLL_SLOW_S


def _learn(text: str, current: str) -> None:
    """拿"现在输入框里的内容"和原话比对，把像回事的改动学进词典。"""
    pairs = diff_words(text, current)
    if not pairs:
        return                                # 没改、或只是补字/删句 —— 正常情况
    if len(pairs) > MAX_PAIRS:
        logger.debug(f'学词：一次改动 {len(pairs)} 处，太多，跳过（不像听错了）')
        return
    keep = [p for p in pairs
            if _word_like(p) and _sounds_alike(p)]
    if len(keep) != len(pairs):
        logger.debug(f'学词：跳过不像"听错了一个词"的改动 '
                     f'{[p for p in pairs if p not in keep]}')
    if not keep:
        return

    report = save_pairs(keep)
    if not report.get('changed'):
        return                                # 已经学过了

    what: List[str] = report.get('what') or []
    logger.info(f'从你的纠正里学到词条: {what}')
    _notify(what)


def _word_like(pair: Tuple[str, str]) -> bool:
    """这一对改动像不像"一个词"（而不是半句话被重写）。"""
    return all(len(p) <= MAX_PAIR_CHARS for p in pair)


def _sounds_alike(pair: Tuple[str, str]) -> bool:
    """
    这一对的**读音**是不是同一串（忽略声调）—— 语音识别写错字就是这个特征。

    这是自动学词最要紧的一道闸，实测非它不可：difflib 会把"你把半句话重写了"
    拆成好几个**短**替换（`周三→周五`、`接口文档→验收报告`、`给我→给他们`），
    每一段都只有三四个字，光靠长度和条数拦不住。但读音一对就露馅了 ——
    那是你换了内容，不是耳朵听岔了。

    判据：
      - 两边都有中文 → 比**音素序列**（声母+韵母，不含声调）。
        声调不算：`老汪 → 老王` 声调不同，但那正是最典型的听错。
      - 纯英文/数字 → 只认"错一两个字母"这种（`wirld → world`）。
      - 中英互改（`派托奇 → PyTorch`）**不自动学** —— 没法比读音，
        想要这种词条请用「词典」页手动加，或者「记录」页的「✏️ 纠正」
        （那边是你亲眼确认过的，不设闸）。
    """
    left, right = pair
    if _has_cjk(left) and _has_cjk(right):
        return _phonemes(left) == _phonemes(right)
    if _has_cjk(left) or _has_cjk(right):
        return False
    return _edit_distance(left.lower(), right.lower()) <= 1


def _has_cjk(text: str) -> bool:
    return any('一' <= c <= '鿿' for c in text)


def _phonemes(text: str) -> List[str]:
    """音素序列，去掉声调（识别错字与声调无关）。"""
    from .hotword.algo_phoneme import get_phoneme_seq

    try:
        return [p.value for p in get_phoneme_seq(text) if not p.is_tone]
    except Exception:
        return [text]


def _edit_distance(a: str, b: str) -> int:
    """两个短词的编辑距离（滚轮 DP，够用就行）。"""
    if abs(len(a) - len(b)) > 2:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _same_control(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    """前后两次读到的焦点控件是不是同一个。读不到类名时保守放行。"""
    b, a = before.get('class'), after.get('class')
    if not b or not a:
        return True
    return b == a


def _still_ours(old: str, current: str) -> bool:
    """
    输入框里现在的内容，是不是"我刚打的那句话被改过的版本"。

    判据：我原话里的字**大部分还在**，而且改动幅度不大。
    不然就是你已经在写别的东西了（或者输入框里本来就有一大段别的文字）。
    """
    if len(current) < len(old) * 0.5:
        return False
    sm = difflib.SequenceMatcher(None, old, current, autojunk=False)
    matched = sum(b.size for b in sm.get_matching_blocks())
    if matched < max(4.0, 0.7 * len(old)):
        return False
    changed = len(old) - matched
    return changed <= max(6.0, 0.35 * len(old))


def _notify(what: List[str]) -> None:
    """
    托盘角落弹一句"学到了"。

    用作者那套 toast（自己的线程 + 自己的 Tk root），和我们不在一个线程上，
    所以不会碰到主线程的 Tk 规则。弹窗失败绝不影响学词本身。
    """
    try:
        from core.ui.toast import toast
        head = '📚 词典 +1' if len(what) == 1 else f'📚 词典 +{len(what)}'
        body = '\n'.join(f'{w.split("（")[0]}' for w in what[:3])
        toast(f'{head}\n{body}', font_size=11, duration=4000,
              bg='#2B3440', initial_width=0.24)
    except Exception as e:
        logger.debug(f'学词提示弹不出来（不影响学词）: {e}')
