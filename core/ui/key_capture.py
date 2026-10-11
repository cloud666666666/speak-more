# coding: utf-8
"""
按一下键就捕获它

设置面板里改快捷键时，用户直接按下想用的那个键就行，不用去下拉框里翻。

## 为什么用 pynput 而不是 Tk 的 <KeyPress>

  1. **鼠标侧键**（x1/x2）Tk 收不到，而应用是支持鼠标快捷键的；
  2. **被应用自己吞掉的键也收不到** —— CapsLock 就是默认快捷键、
     `suppress=True`，应用的低级钩子会先把它吃掉，Tk 那边根本等不到事件。
     （更糟的是应用还会就地开始录音。）

所以得先把应用的监听器停下来，再用 pynput 收。pynput 是应用本来就有的依赖，
键盘（含字母键）和鼠标侧键都能覆盖 —— 实测 `SHORTCUT_KEYS` 里 79 个键盘键
pynput 全有，字母键以 `KeyCode(char='a')` 的形式到达。

## ⚠️ 为什么不能直接调 ShortcutManager.stop()

`stop()` 除了停监听器还会 `self._pool.shutdown(wait=False)`，而 `start()`
**不会重建线程池**。也就是说 `stop()` 之后再 `start()`，快捷键看着恢复正常了，
实际上下一次按键就会抛
`RuntimeError: cannot schedule new futures after shutdown` ——
面板显示一切正常，用户回去一按快捷键就报错。

（`settings_apply._rebuild()` 之所以能写 `stop()` + `start()`，是因为它紧接着
 建的是**全新**的 `ShortcutManager` 实例，线程池是新 `__init__` 出来的。）

所以这里**只停/建监听器，绝不碰线程池**。见 `pause_shortcuts()`。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

# 面板里显示的漂亮名字。键名本身是应用内部用的（caps_lock / ctrl_l / x2），
# 直接摆给用户看太生硬。
# 应用支持的鼠标键。
#
# ⚠️ **只能收 x1/x2，绝不能收 left/right/middle。**
# 应用内部的按键名里 `left` / `right` 指的是**方向键**（见 SHORTCUT_KEYS
# 里的 up/down/left/right），而 pynput 的鼠标左/右键名字**也叫 left/right**。
# 名字一撞，用户在面板里随便点一下鼠标就会被"捕获"成左方向键 ——
# 静默改掉他的快捷键，而且看着还挺合理。同理 `middle`/`up`/`down` 也不收。
# （这一条是测试抓出来的，不是我想到的。）
_MOUSE_BUTTONS = frozenset({'x1', 'x2'})

_KEY_LABELS: Dict[str, str] = {
    'caps_lock': 'Caps Lock', 'num_lock': 'Num Lock', 'scroll_lock': 'Scroll Lock',
    'ctrl_l': '左 Ctrl', 'ctrl_r': '右 Ctrl', 'ctrl': '左 Ctrl',
    'shift': '左 Shift', 'shift_r': '右 Shift', 'shift_l': '左 Shift',
    'alt_l': '左 Alt', 'alt_gr': '右 Alt', 'alt_r': '右 Alt', 'alt': '左 Alt',
    'cmd': '左 Win', 'cmd_l': '左 Win', 'cmd_r': '右 Win',
    'space': '空格', 'enter': '回车', 'tab': 'Tab', 'backspace': '退格',
    'delete': 'Delete', 'insert': 'Insert', 'esc': 'Esc',
    'home': 'Home', 'end': 'End', 'page_up': 'Page Up', 'page_down': 'Page Down',
    'print_screen': 'Print Screen', 'pause': 'Pause', 'menu': '菜单键',
    'up': '↑', 'down': '↓', 'left': '←', 'right': '→',
    'x1': '鼠标侧键 1', 'x2': '鼠标侧键 2',
    # pynput 会报出来、但应用不支持的名字。加上中文名是为了让
    # 「XXX 不能用作快捷键」这句提示读起来不像乱码。
    'media_play_pause': '媒体播放/暂停键', 'media_next': '下一曲键',
    'media_previous': '上一曲键', 'media_stop': '媒体停止键',
    'media_volume_up': '音量加键', 'media_volume_down': '音量减键',
    'media_volume_mute': '静音键',
}

# 应用列表里没有、但 pynput 会报出来的通用名 —— 归一到列表里有的写法
_ALIASES: Dict[str, str] = {
    'ctrl': 'ctrl_l',        # 列表只有左右之分，没有通用名
    'alt': 'alt_l',
    'alt_r': 'alt_gr',       # 列表里右 Alt 叫 alt_gr
    'shift_l': 'shift',
    'cmd_l': 'cmd',
}


@dataclass
class CaptureResult:
    """一次捕获的结果。"""
    key: Optional[str] = None       # 应用能用的键名；None 表示没捕获到
    label: str = ''                 # 给人看的名字
    reason: str = ''                # key 为 None 时，为什么不行

    @property
    def ok(self) -> bool:
        return bool(self.key)


def key_label(name: str) -> str:
    """键名 -> 界面上的显示名。"""
    n = (name or '').strip().lower()
    if not n:
        return '（未设置）'
    if n in _KEY_LABELS:
        return _KEY_LABELS[n]
    if n.startswith('f') and n[1:].isdigit():
        return n.upper()
    if len(n) == 1:
        return n.upper()
    return name


# ============================================================
#  暂停 / 恢复应用的快捷键监听
# ============================================================

def pause_shortcuts(app: Any) -> bool:
    """
    只停监听器，**不碰线程池**。返回是否真的停了点什么。

    见模块头：直接用 `ShortcutManager.stop()` 会把线程池关掉，
    而 `start()` 不重建它，之后一按快捷键就崩。
    """
    sm = getattr(app, 'shortcut', None)
    if sm is None:
        return False

    paused = False
    for attr in ('keyboard_listener', 'mouse_listener'):
        listener = getattr(sm, attr, None)
        if listener is None:
            continue
        try:
            listener.stop()
        except Exception:
            pass
        # 必须置 None：ShortcutManager.start() 是靠
        # `if self.xxx_listener and self.xxx_listener.is_alive()` 判断要不要重建的，
        # 留着一个已停的 listener 对象，它就不会重建了。
        try:
            setattr(sm, attr, None)
        except Exception:
            pass
        paused = True
    return paused


def resume_shortcuts(app: Any) -> bool:
    """把监听器重新建起来（走 ShortcutManager.start()，它自己会判断要不要建）。"""
    sm = getattr(app, 'shortcut', None)
    if sm is None:
        return False
    try:
        sm.start()
        return True
    except Exception:
        return False


# ============================================================
#  捕获
# ============================================================

def _name_of(key: Any) -> Optional[str]:
    """pynput 的按键对象 -> 应用能用的键名。不认识返回 None。"""
    try:
        from core.ui import settings_schema as S
        known = set(S.SHORTCUT_KEYS)
    except Exception:
        return None

    try:
        name = getattr(key, 'name', None)
        if name:
            name = str(name).lower()
            if name in known:
                return name
            if name in _ALIASES and _ALIASES[name] in known:
                return _ALIASES[name]

        # 字母键：pynput 给的是 KeyCode(char='a')，没有 .name
        char = getattr(key, 'char', None)
        if char and len(char) == 1:
            low = char.lower()
            if low in known:
                return low
    except Exception:
        pass
    return None


def _describe(key: Any) -> str:
    """pynput 的按键对象 -> 给人看的名字（用在"这个键不支持"的提示里）。"""
    try:
        name = getattr(key, 'name', None)
        if name:
            return key_label(str(name))
        char = getattr(key, 'char', None)
        if char:
            return str(char).upper()
    except Exception:
        pass
    return str(key)


def unsupported_reason(label: str) -> str:
    """按了应用不支持的键时的提示语。单独拎出来是为了能直接测。"""
    return f'「{label}」不能用作快捷键，换一个键试试。'


def press_result(key: Any) -> Optional[CaptureResult]:
    """
    一个按键到达时该怎么处理。

    Returns:
        CaptureResult = 捕获结束；
        None          = **继续等**（Esc 属于这种，见下面的说明）。
    """
    name = _name_of(key)

    if name == 'esc':
        # ⚠️ **Esc 一律不捕获，交给 Tk 那边的 <Escape> 绑定处理。**
        #
        # 不这么做会有竞态：按 Esc 时，我们的捕获器会先把 'esc' 塞进结果队列，
        # 而 Tk 的 <Escape> 绑定也会同时触发。谁先跑到不一定 ——
        # 要是捕获结果先被取走，这一行的按键就被设成了 esc；紧接着
        # Tk 的 Esc 处理发现"已经不在捕获态了"，于是**把整个面板关掉**，
        # 用户改了一半的设置全丢。
        #
        # 所以这里直接吞掉 Esc 继续等，让 Esc 只有一条路径：
        # core/ui/settings_dialog._on_escape（捕获中 = 取消捕获，否则 = 关面板）。
        # 真想把它设成快捷键，用右边的 ▾ 菜单选。
        return None

    if name:
        return CaptureResult(key=name, label=key_label(name))

    # 按了应用不支持的键（比如媒体键）。**不要一直等下去** ——
    # 用户按了没反应会以为坏了，直接把原因说清楚。
    label = _describe(key)
    return CaptureResult(label=label, reason=unsupported_reason(label))


def _button_name(button: Any) -> Optional[str]:
    """
    鼠标键 -> 应用能用的名字。**只认 x1/x2**。

    见 `_MOUSE_BUTTONS` 的说明：left/right 和"左/右方向键"撞名，
    这里放行的话用户点一下鼠标就会被当成按了方向键。
    """
    try:
        name = str(getattr(button, 'name', '')).lower()
        return name if name in _MOUSE_BUTTONS else None
    except Exception:
        return None


def capture_next_key(app: Any = None, timeout: float = 15.0,
                     cancel: Optional[threading.Event] = None) -> CaptureResult:
    """
    **阻塞**到用户按下一个键（或点一下鼠标侧键），返回结果。

    调用方要在**后台线程**里调它 —— 它会一直等。

    Args:
        app:      宿主 app。传了就会在捕获期间把快捷键监听停掉（必须的，
                  否则 CapsLock 这类会被应用自己吞掉，捕获器什么都收不到）。
        timeout:  最多等多少秒。
        cancel:   调用方想中途放弃时 set 它（面板被关掉时用）。

    Returns:
        CaptureResult。`key` 为 None 表示超时/取消/按了不支持的键，
        `reason` 里是给人看的说明。
    """
    try:
        from pynput import keyboard, mouse
    except Exception as e:
        return CaptureResult(reason=f'按键捕获不可用（缺少 pynput）: {e}')

    box: Dict[str, CaptureResult] = {}
    done = threading.Event()

    def _finish(res: CaptureResult) -> None:
        if not done.is_set():
            box['r'] = res
            done.set()

    def on_press(key):
        res = press_result(key)
        if res is None:
            return True               # Esc：继续等，交给 Tk 那边处理
        _finish(res)
        return False                  # 返回 False 让 pynput 停掉监听

    def on_click(x, y, button, pressed):
        if not pressed:
            return True
        name = _button_name(button)
        if name:
            _finish(CaptureResult(key=name, label=key_label(name)))
            return False
        # 左/中/右键不放行 —— 它们是正常点击，不该被当成"按了不支持的键"，
        # 否则用户点一下别的地方就被判失败。继续等。
        return True

    paused = pause_shortcuts(app) if app is not None else False
    kb = ms = None
    try:
        kb = keyboard.Listener(on_press=on_press, suppress=False)
        ms = mouse.Listener(on_click=on_click, suppress=False)
        kb.start()
        ms.start()

        deadline = time.monotonic() + max(1.0, float(timeout))
        while not done.is_set():
            if cancel is not None and cancel.is_set():
                break
            if time.monotonic() >= deadline:
                break
            done.wait(0.05)
    except Exception as e:
        return CaptureResult(reason=f'按键捕获失败: {e}')
    finally:
        for listener in (kb, ms):
            if listener is None:
                continue
            try:
                listener.stop()
            except Exception:
                pass
        # **必须恢复** —— 无论成功、超时、还是异常。漏了这一步用户的快捷键
        # 就一直是哑的，而且面板上看不出任何异常。
        if paused:
            resume_shortcuts(app)

    if 'r' in box:
        return box['r']
    if cancel is not None and cancel.is_set():
        return CaptureResult(reason='已取消')
    return CaptureResult(reason='没等到按键（超时了），再试一次。')
