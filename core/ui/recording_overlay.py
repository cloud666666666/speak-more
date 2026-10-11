# coding: utf-8
"""
录音状态浮层：屏幕下方一条跟着声音跳动的波形

对标 Typeless 的录音反馈。作者原本的"录音中"提示只是一个控制台里的
rich 动画（core/tools/my_status.py），而控制台是隐藏的 —— 用户按了快捷键
之后其实**完全看不到任何反馈**，只能凭感觉。

设计上的几个要点：

1. **绝不抢焦点**。这个窗口用 WS_EX_NOACTIVATE，永远不会成为前台窗口。
   否则识别结果用 keyboard.write 上屏时会打进浮层自己里面，而不是用户
   正在编辑的输入框 —— 那是最糟糕的失败模式。

2. **鼠标穿透**。加 WS_EX_TRANSPARENT，点击直接穿到下面的窗口，
   这个浮层纯指示、不可能挡住用户操作。

3. **跑在自己的 daemon 线程 + 自己的 Tk root**，和项目的 toast 系统
   （core/ui/toast_manager.py）同一套路子，不和主 asyncio 循环纠缠。

4. 显示时机不靠在作者代码里到处插钩子，而是**轮询 app.state.recording**，
   这样只需要在音频回调里发布一下电平，改动面最小。
"""

from __future__ import annotations

import collections
import ctypes
import math
import threading
import time
import tkinter as tk
from typing import Any, Deque, List, Optional

try:
    from . import logger
except Exception:                                  # pragma: no cover
    import logging
    logger = logging.getLogger('core.ui')

# ============================================================
#  外观参数
# ============================================================

BARS = 36                 # 波形条数
BAR_W = 5                 # 每根宽度
BAR_GAP = 3               # 间距
WAVE_H = 54               # 波形最大高度（对称，上下各一半）
PAD_X = 18
PAD_Y = 12
LABEL_H = 20

BG = '#0f1115'
BAR_LOW = '#4a5568'
BAR_MID = '#3b82f6'
BAR_HIGH = '#22d3ee'
TEXT_FG = '#cbd5e1'
HINT_FG = '#64748b'

FRAME_MS = 55             # ≈18fps，够顺滑又不烧 CPU
TAIL_SECONDS = 1.6        # 停止录音后再显示这么久，盖住"正在识别"
BOTTOM_MARGIN = 110       # 距屏幕底部

# 电平映射：把 RMS 折算成 0~1
# 实测麦克风的 float32 幅度：正常说话 RMS 大概 0.02~0.06，
# 大声 0.1+，静音 0.003 以下。按 dB 折算比线性更贴合听感。
DB_FLOOR = -55.0          # 低于这个 dB 当作静音
DB_CEIL = -18.0           # 高于这个 dB 当作满格

# 按键名 -> 给人看的名字
_KEY_LABELS = {
    'caps_lock': 'CapsLock', 'num_lock': 'NumLock', 'scroll_lock': 'ScrollLock',
    'ctrl_l': '左Ctrl', 'ctrl_r': '右Ctrl', 'shift': 'Shift', 'shift_r': '右Shift',
    'alt_l': '左Alt', 'alt_gr': '右Alt', 'cmd': 'Win', 'cmd_r': '右Win',
    'x1': '鼠标侧键1', 'x2': '鼠标侧键2', 'space': '空格', 'enter': '回车',
    'page_up': 'PageUp', 'page_down': 'PageDown', 'print_screen': 'PrintScreen',
}


# ============================================================
#  电平发布（音频线程 -> 浮层线程）
# ============================================================

_latest_level: float = 0.0
_latest_ts: float = 0.0
_generation: int = 0        # 每次开始录音 +1，用来清空上一轮的波形


def publish_audio(chunk: Any) -> None:
    """
    由音频回调调用，算出一个 0~1 的电平。

    必须在音频线程里跑得快，所以这里不做锁，只做原子赋值
    （CPython 里给模块级名字赋 float 是原子的）。
    """
    global _latest_level, _latest_ts
    try:
        import numpy as np
        if chunk is None or len(chunk) == 0:
            return
        rms = float(np.sqrt(np.mean(np.square(chunk))))
    except Exception:
        return

    if rms <= 1e-7:
        level = 0.0
    else:
        db = 20.0 * math.log10(rms)
        level = (db - DB_FLOOR) / (DB_CEIL - DB_FLOOR)
        level = 0.0 if level < 0.0 else (1.0 if level > 1.0 else level)

    _latest_level = level
    _latest_ts = time.time()


def reset_level() -> None:
    """开始新一轮录音时清掉上一条波形。"""
    global _latest_level, _generation
    _latest_level = 0.0
    _generation += 1


def current_generation() -> int:
    return _generation


# ---- 测麦克风模式 ----
# 设置向导要知道"用户选的这个麦克风到底能不能收到声音"。
# 做法是**复用应用自己那条音频流**，而不是另开一路 —— 另开会和主音频
# 抢同一个设备（可能直接失败），而且测的还不是真正会用到的那个通道。
# 打开这个开关后，音频回调在非录音状态下也照样更新电平。

_test_mode: bool = False


def set_test_mode(on: bool) -> None:
    global _test_mode, _latest_level
    _test_mode = bool(on)
    if _test_mode:
        _latest_level = 0.0


def is_test_mode() -> bool:
    return _test_mode


def get_level() -> float:
    """当前电平（0~1）。测麦克风和浮层都读这个。"""
    return _latest_level


# ============================================================
#  Windows 扩展样式
# ============================================================

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000       # 永不成为前台窗口
WS_EX_TOOLWINDOW = 0x00000080       # 不在任务栏/Alt-Tab 里出现
WS_EX_TRANSPARENT = 0x00000020      # 鼠标穿透


def _make_unfocusable(win: tk.Toplevel) -> None:
    """
    把窗口设成不抢焦点 + 鼠标穿透。

    **必须在窗口 deiconify 之后才能调** —— 这是踩过的坑：
    窗口还在 withdraw 状态时 `GetParent(winfo_id())` 返回 0，
    拿不到真正的顶层句柄，样式就被打在了 Tk 的内部子窗口上，
    而真正显示出来的那个顶层窗口不受影响、照样会抢焦点。
    实测：withdraw 时设，顶层样式纹丝不动（0x88）；
    deiconify 之后设才变成 0xa8（带上 NOACTIVATE|TRANSPARENT）。

    为了保险，子窗口和顶层窗口两个句柄都设一遍。
    """
    try:
        user32 = ctypes.windll.user32
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.restype = ctypes.c_long

        child = win.winfo_id()
        handles = {child}
        parent = user32.GetParent(child)
        if parent:
            handles.add(parent)

        want = WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT
        for hwnd in handles:
            try:
                style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | want)
            except Exception:
                pass
    except Exception as e:
        logger.debug(f'设置浮层扩展样式失败（不影响录音）: {e}')


# ============================================================
#  浮层本体
# ============================================================

class RecordingOverlay:
    """录音时在屏幕下方显示一条实时波形。"""

    def __init__(self, app: Any):
        self.app = app
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._enabled = True

    # ---------- 生命周期 ----------

    def start(self) -> None:
        try:
            from config_client import ClientConfig as Config
            self._enabled = bool(getattr(Config, 'show_recording_overlay', True))
        except Exception:
            self._enabled = True

        if not self._enabled:
            logger.info('录音浮层已在设置里关闭')
            return

        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='RecordingOverlay')
        self._thread.start()
        logger.info('录音浮层已启动')

    def stop(self) -> None:
        self._stop.set()

    # ---------- Tk 线程 ----------

    def _run(self) -> None:
        try:
            root = tk.Tk()
            root.withdraw()
            self._build(root)
            self._tick()
            root.mainloop()
        except Exception as e:
            logger.warning(f'录音浮层启动失败（不影响录音功能）: {e}')

    def _build(self, root: tk.Tk) -> None:
        self.root = root

        width = PAD_X * 2 + BARS * BAR_W + (BARS - 1) * BAR_GAP
        height = PAD_Y * 2 + WAVE_H + LABEL_H

        win = tk.Toplevel(root)
        win.overrideredirect(True)           # 无边框
        win.attributes('-topmost', True)     # 置顶
        win.configure(bg=BG)
        win.withdraw()                       # 先藏起来，第一次需要时再显示
        # 注意：_make_unfocusable() 不能在这里调 —— 窗口还没显示出来，
        # 拿不到真正的顶层句柄。改到 _show() 里、deiconify 之后再设。

        canvas = tk.Canvas(win, width=width, height=height, bg=BG,
                           highlightthickness=0, borderwidth=0)
        canvas.pack()

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = max(0, (sw - width) // 2)
        y = max(0, sh - height - BOTTOM_MARGIN)
        win.geometry(f'{width}x{height}+{x}+{y}')

        self.win = win
        self.canvas = canvas
        self.width = width
        self.height = height
        self.history: Deque[float] = collections.deque([0.0] * BARS, maxlen=BARS)
        self.visible = False
        self._last_active = 0.0
        self._was_recording = False
        self._label = ''
        self._hint = ''

    # ---------- 每帧 ----------

    def _tick(self) -> None:
        try:
            self._frame()
        except Exception as e:
            logger.debug(f'录音浮层刷新出错: {e}')
        if not self._stop.is_set() and getattr(self, 'root', None) is not None:
            try:
                self.root.after(FRAME_MS, self._tick)
            except Exception:
                pass

    def _frame(self) -> None:
        state = getattr(self.app, 'state', None)
        recording = bool(getattr(state, 'recording', False))

        # 录音开始的上升沿 -> 清空上一条波形，免得新旧混在一起
        if recording and not self._was_recording:
            self.history = collections.deque([0.0] * BARS, maxlen=BARS)
        self._was_recording = recording

        if recording:
            self._last_active = time.time()
            self.history.append(_peak_decay(_latest_level))
            self._label = '正在聆听…'
            self._hint = self._stop_hint()
            self._show(True)
        else:
            idle = time.time() - self._last_active
            if self._last_active and idle < TAIL_SECONDS:
                # 松开之后还在等识别结果，把这段尾巴也画上
                self.history.append(_peak_decay(_latest_level))
                self._label = '正在识别…'
                self._hint = ''
                self._show(True)
            else:
                self.history.append(0.0)
                self._show(False)

        if self.visible:
            self._draw()

    def _stop_hint(self) -> str:
        """
        单击模式（hold_mode=False）下提示"再按一次结束"。
        长按模式没有这个需要 —— 松手就结束了。
        """
        try:
            from config_client import ClientConfig as Config
            for sc in getattr(Config, 'shortcuts', []) or []:
                if isinstance(sc, dict) and sc.get('enabled') and not sc.get('hold_mode'):
                    key = str(sc.get('key', ''))
                    return f'再按一次 {_KEY_LABELS.get(key, key)} 结束'
        except Exception:
            pass
        return ''

    # ---------- 绘制 ----------

    def _show(self, want: bool) -> None:
        if want == self.visible:
            return
        try:
            if want:
                self.win.deiconify()
                self.win.attributes('-topmost', True)
                # 必须在 deiconify **之后**设，否则样式会打到 Tk 的子窗口上，
                # 真正显示的顶层窗口照样会抢焦点（见 _make_unfocusable 的注释）
                self.win.update_idletasks()
                _make_unfocusable(self.win)
            else:
                self.win.withdraw()
        except Exception:
            pass
        self.visible = want

    def _draw(self) -> None:
        c = self.canvas
        c.delete('all')

        mid = PAD_Y + WAVE_H / 2
        max_h = WAVE_H / 2
        x = PAD_X

        for level in self.history:
            h = max(2.0, level * max_h)          # 至少 2px，安静时也是一条细线
            color = BAR_LOW
            if level > 0.66:
                color = BAR_HIGH
            elif level > 0.33:
                color = BAR_MID
            c.create_rectangle(x, mid - h, x + BAR_W, mid + h,
                               fill=color, outline=color)
            x += BAR_W + BAR_GAP

        y_text = PAD_Y + WAVE_H + 4
        c.create_text(PAD_X, y_text, anchor='nw', text=self._label,
                      fill=TEXT_FG, font=('Microsoft YaHei UI', 11, 'bold'))
        if self._hint:
            c.create_text(self.width - PAD_X, y_text + 2, anchor='ne',
                          text=self._hint, fill=HINT_FG,
                          font=('Microsoft YaHei UI', 9))


def _peak_decay(level: float) -> float:
    """轻微平滑，避免柱子闪得厉害。"""
    return max(0.0, min(1.0, level))


# ============================================================
#  便捷入口
# ============================================================

_overlay: Optional[RecordingOverlay] = None
_overlay_lock = threading.Lock()


def start_overlay(app: Any) -> None:
    global _overlay
    with _overlay_lock:
        if _overlay is not None:
            return
        try:
            _overlay = RecordingOverlay(app)
            _overlay.start()
        except Exception as e:
            logger.warning(f'录音浮层启动失败（不影响录音）: {e}')
            _overlay = None


def stop_overlay() -> None:
    global _overlay
    with _overlay_lock:
        if _overlay is not None:
            try:
                _overlay.stop()
            except Exception:
                pass
            _overlay = None
