# coding: utf-8
"""
录音状态浮层：屏幕下方一条跟着声音起伏的**波形带**

对标 Typeless 的录音反馈。作者原本的"录音中"提示只是一个控制台里的
rich 动画（core/tools/my_status.py），而控制台是隐藏的 —— 用户按了快捷键
之后其实**完全看不到任何反馈**，只能凭感觉。

波形画法是 Pillow 渲染 + 贴到 Tk 画布上（Tk 的 Canvas 没有抗锯齿，
直接画曲线会毛糙）。做法是**放大 3 倍绘制再缩回来**当抗锯齿，顺便拿到了
渐变填充和柔光边缘 —— 这是"一条连续的带"，不是一排竖条。

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

5. Pillow 万一不可用，自动退回"一排竖条"的老画法（功能不受影响）。
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

# 波形渲染用的图形库（项目本来就依赖 Pillow，托盘的图标就是它做的）
try:
    from PIL import Image, ImageDraw, ImageFilter, ImageTk
    _HAS_PIL = True
except Exception:                                  # pragma: no cover
    _HAS_PIL = False

# ============================================================
#  外观参数
# ============================================================

SAMPLES = 46              # 波形保留多少个采样点（越多越细腻）
WAVE_W = 300              # 波形带宽度（像素）
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

# ---- 波形带的外观 ----
SS = 3                    # 超采样倍数（放大画再缩回来 = 抗锯齿）
MIN_HALF = 1.6            # 安静时的半高（像素），保证始终是一条细线
DENSE_PER_SEG = 5         # 每两个采样点之间插多少个点（点越密曲线越顺）
GLOW = 7                  # 外发光半径（像素，0 = 不发光）
WAVE_GAIN = 0.6           # 波形能长多高（1.0 = 占满 WAVE_H；越小越秀气）
                          # 0.6 是量出来的"看得清起伏又不糊成一坨"的档位，
                          # 想要更接近 Typeless 那种细线感就调到 0.35

# 电平映射：把 RMS 折算成 0~1
# 实测麦克风的 float32 幅度：正常说话 RMS 大概 0.02~0.06，
# 大声 0.1+，静音 0.003 以下。按 dB 折算比线性更贴合听感。
DB_FLOOR = -55.0          # 低于这个 dB 当作静音
DB_CEIL = -18.0           # 高于这个 dB 当作满格


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

        width = PAD_X * 2 + WAVE_W
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
        self.history: Deque[float] = collections.deque([0.0] * SAMPLES,
                                                       maxlen=SAMPLES)
        self.visible = False
        self._last_active = 0.0
        self._was_recording = False
        self._label = ''
        self._hint = ''
        # 波形带的渲染缓存（渐变底和外发光图只做一次）
        self._photo = None                 # 当前贴上去的图（必须留引用）
        self._bg_cache = None
        self._glow_cache = None

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
            self.history = collections.deque([0.0] * SAMPLES, maxlen=SAMPLES)
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

        # 波形：优先画"连续的带"（对标 Typeless），Pillow 不在就退回竖条
        if not (self._wave_supported() and self._draw_wave()):
            self._draw_bars()

        y_text = PAD_Y + WAVE_H + 4
        c.create_text(PAD_X, y_text, anchor='nw', text=self._label,
                      fill=TEXT_FG, font=('Microsoft YaHei UI', 11, 'bold'))
        if self._hint:
            c.create_text(self.width - PAD_X, y_text + 2, anchor='ne',
                          text=self._hint, fill=HINT_FG,
                          font=('Microsoft YaHei UI', 9))

    # ---------- 画法一：连续的波形带 ----------

    @staticmethod
    def _wave_supported() -> bool:
        return _HAS_PIL

    def _dense_levels(self) -> List[float]:
        """
        把采样点插值成一条稠密的曲线（每个点的**半高**，0~1）。

        用余弦插值：相邻两点之间走 S 形 —— 连线自然，又不会像三次样条那样
        **过冲**（过冲会在安静段戳出小尖刺，看着像杂音）。
        """
        hist = list(self.history)
        if len(hist) < 2:
            return hist
        out: List[float] = []
        for i in range(len(hist) - 1):
            a, b = hist[i], hist[i + 1]
            for k in range(DENSE_PER_SEG):
                t = k / DENSE_PER_SEG
                out.append(a + (b - a) * (1.0 - math.cos(math.pi * t)) / 2.0)
        out.append(hist[-1])
        return out

    def _wave_polygon(self, levels: List[float], scale: int) -> List[tuple]:
        """由"半高序列"生成上下对称的闭合多边形（坐标已乘 scale）。"""
        n = len(levels)
        if n < 2:
            return []
        step = (WAVE_W * scale) / (n - 1)
        mid = (PAD_Y + WAVE_H / 2) * scale
        top, bot = [], []
        for i, lv in enumerate(levels):
            x = PAD_X * scale + i * step
            h = max(MIN_HALF, lv * (WAVE_H / 2) * WAVE_GAIN) * scale
            top.append((x, mid - h))
            bot.append((x, mid + h))
        return top + list(reversed(bot))

    def _gradient(self, level: float):
        """纵向渐变：上缘亮 → 中间实 → 下缘亮；主色随音量在蓝→青之间走。"""
        key = round(level, 2)
        if getattr(self, '_grad_key', None) == key and \
                getattr(self, '_grad_cache', None) is not None:
            return self._grad_cache

        base = _mix_hex(BAR_MID, BAR_HIGH, key)          # 已经是 RGB 元组
        edge = _mix_rgb(base, _hex_to_rgb('#ffffff'), 0.45)
        h_px = (PAD_Y * 2 + WAVE_H) * SS
        col = Image.new('RGB', (1, h_px))
        px = col.load()
        for y in range(h_px):
            t = y / max(1, h_px - 1)
            # 0 → 1 → 0 的对称权重：两头亮、中间实
            w = abs(t - 0.5) * 2.0
            px[0, y] = _mix_rgb(base, edge, w ** 2)
        self._grad_cache = col.resize(
            (self.width * SS, h_px), Image.BILINEAR)
        self._grad_key = key
        return self._grad_cache

    def _render_wave_image(self):
        """
        渲染波形带的 PIL 图；画不出来返回 None。

        不碰 Tk，所以可以离屏生成（自测 / 预览图就是用它出的）。
        """
        levels = self._dense_levels()
        if len(levels) < 2:
            return None
        try:
            W = self.width
            H = PAD_Y * 2 + WAVE_H

            if self._bg_cache is None:
                self._bg_cache = Image.new('RGB', (W * SS, H * SS), BG)
            img = self._bg_cache.copy()
            poly = self._wave_polygon(levels, SS)

            # 蒙版：波形带的形状
            mask = Image.new('L', (W * SS, H * SS), 0)
            ImageDraw.Draw(mask).polygon(poly, fill=255)

            # 外发光：把蒙版模糊一圈铺一层主色（营造"在发光"的感觉）
            if GLOW > 0:
                glow = mask.filter(ImageFilter.GaussianBlur(GLOW * SS / 2.0))
                glow = glow.point(lambda v: int(v * 0.5))
                img.paste(Image.new('RGB', img.size, BAR_MID), (0, 0), glow)

            # 渐变填充（按平均音量取色）+ 缩小回原尺寸 = 抗锯齿
            mean_level = sum(levels) / len(levels)
            img = Image.composite(self._gradient(mean_level), img, mask)
            return img.resize((W, H), Image.LANCZOS)
        except Exception as e:
            logger.debug(f'波形带渲染失败: {e}')
            return None

    def _draw_wave(self) -> bool:
        """把波形带贴到画布上。返回 False 表示没画成（调用方退回竖条画法）。"""
        img = self._render_wave_image()
        if img is None:
            return False
        try:
            self._photo = ImageTk.PhotoImage(img)      # 必须留引用，否则不显示
            self.canvas.create_image(0, 0, image=self._photo, anchor='nw')
            return True
        except Exception as e:
            logger.debug(f'波形带贴图失败，退回竖条画法: {e}')
            return False

    # ---------- 画法二：竖条（Pillow 不可用时的兜底） ----------

    def _draw_bars(self) -> None:
        c = self.canvas
        hist = list(self.history) or [0.0]
        slot = WAVE_W / len(hist)
        bar_w = max(2.0, slot * 0.62)
        mid = PAD_Y + WAVE_H / 2
        max_h = WAVE_H / 2
        x = PAD_X
        for level in hist:
            h = max(1.5, level * max_h)
            color = BAR_LOW
            if level > 0.66:
                color = BAR_HIGH
            elif level > 0.33:
                color = BAR_MID
            c.create_rectangle(x, mid - h, x + bar_w, mid + h,
                               fill=color, outline=color)
            x += slot


def _peak_decay(level: float) -> float:
    """轻微平滑，避免柱子闪得厉害。"""
    return max(0.0, min(1.0, level))


# ---- 颜色小工具（画渐变用） ----

def _hex_to_rgb(value: str) -> tuple:
    v = value.lstrip('#')
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def _mix_rgb(a: tuple, b: tuple, t: float) -> tuple:
    """两个颜色按 t（0~1）混合。"""
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _mix_hex(a: str, b: str, t: float) -> tuple:
    return _mix_rgb(_hex_to_rgb(a), _hex_to_rgb(b), t)


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
