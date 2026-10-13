# coding: utf-8
"""
「测试麦克风」小组件

首次向导和设置面板都要用，所以抽出来共用一份，避免两处各写一遍以后漂移。

包含：按钮「测试麦克风（3 秒）」+ 实时电平条 + 结论文字。

两个关键实现点（都是踩过的）：

1. **测之前必须先把选中的设备应用上去**（改配置 + 重开音频流），
   否则测的是**旧设备** —— 用户换了个麦点测试，看到的却是旧麦的电平，
   等于没测，而且他还会以为新设备是好的。所以要注入一个 `mic_apply`。

2. **复用应用自己那条音频流**（`recording_overlay.set_test_mode`），
   不另开一路 InputStream —— 另开会和主音频流抢同一个设备，
   而且测的还不是真正会用到的那个通道。
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Optional

try:
    from . import logger
except Exception:                                  # pragma: no cover
    import logging
    logger = logging.getLogger('core.ui')

# 电平档位（recording_overlay 输出的是 0~1）
LEVEL_OK = 0.25        # 高于这个：能清楚听到
LEVEL_QUIET = 0.08     # 高于这个：能听到但很轻


class MicTestPanel(ttk.Frame):
    """
    Args:
        get_device: () -> str          取界面上当前选中的设备名
        mic_apply:  (name) -> bool     把选择应用上去（改配置 + 重开音频流）
        bg:         背景色，跟着父容器走
        font:       结论文字用的字体
    """

    DURATION = 3.0
    BAR_WIDTH = 230
    BAR_HEIGHT = 16

    def __init__(self, master: tk.Misc,
                 get_device: Callable[[], str],
                 mic_apply: Optional[Callable[[str], bool]] = None,
                 bg: str = 'white',
                 font: Any = None,
                 accent: str = '#1d6fd0',
                 **kw: Any):
        super().__init__(master, **kw)
        self._get_device = get_device
        self._mic_apply = mic_apply
        self._bg = bg
        self._font = font or ('Microsoft YaHei UI', 9)
        self._accent = accent
        self._testing = False

        # [补丁] 工作线程 → UI 线程的消息队列。
        # **绝不能从工作线程直接碰 Tk**：tkinter 在那种情况下抛
        # RuntimeError('main thread is not in main loop')，而原来的 _ui() 把它
        # 静默吞掉了 —— 表现就是"点了测试麦克风，结论一直是『请对着麦克风说几句话…』"，
        # 进度条也不动。正常模式与 --settings 独立模式都中招（实测复现）。
        # 现在改成：工作线程只往队列里放，UI 线程用 after 轮询取。
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._polling = False

        # 第一行：按钮 + 电平条；第二行：结论。结论单独一行才不会把行撑得很宽
        self.columnconfigure(1, weight=1)

        self.btn = ttk.Button(self, text='测试麦克风（3 秒）', width=18,
                              command=self.start)
        self.btn.grid(row=0, column=0, sticky='w')

        self.bar = tk.Canvas(self, width=self.BAR_WIDTH, height=self.BAR_HEIGHT,
                             bg='#e5e7eb', highlightthickness=0, borderwidth=0)
        self.bar.grid(row=0, column=1, sticky='w', padx=(10, 0))
        self.bar_fill = self.bar.create_rectangle(0, 0, 0, self.BAR_HEIGHT,
                                                  fill=accent, outline='')

        self.verdict = tk.Label(self, text='', font=self._font, bg=bg,
                                fg='#6b7280', wraplength=560, justify='left',
                                anchor='w')
        self.verdict.grid(row=1, column=0, columnspan=2, sticky='w',
                          pady=(8, 0))

    # ---------------- 对外 ----------------

    @property
    def is_testing(self) -> bool:
        return self._testing

    def start(self) -> None:
        if self._testing:
            return
        self._testing = True
        try:
            self.btn.configure(state='disabled')
            self.verdict.configure(text='请对着麦克风说几句话…', fg='#6b7280')
            self.draw_bar(0.0)
        except Exception:
            pass
        self._start_polling()
        threading.Thread(target=self._worker, daemon=True,
                         name='MicTest').start()

    # ---------------- 内部 ----------------

    def _start_polling(self) -> None:
        """开始轮询（start() 由按钮回调触发，本来就在 Tk 线程上跑）。"""
        if self._polling:
            return
        self._polling = True
        self._poll()

    def _poll(self) -> None:
        """
        只在 Tk 线程上跑：把工作线程投递的结果刷到界面。

        为什么不让工作线程直接 `after(0, ...)`：tkinter 会抛
        RuntimeError('main thread is not in main loop') —— 异常一旦被吞，
        用户看到的就是"点了没反应"。本面板原来就是这么坏的。
        """
        finished = False
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]
                if kind == 'level':
                    self.draw_bar(msg[1])
                elif kind == 'finish':
                    self.finish(*msg[1:])
                    finished = True
        except queue.Empty:
            pass
        except Exception:
            pass

        if finished:
            self._polling = False
            return
        if self._polling:
            try:
                self.after(60, self._poll)
            except Exception:
                self._polling = False          # 面板已经销毁

    def _post(self, *msg: Any) -> None:
        """工作线程调用：只往队列里放，**绝不碰 Tk**。"""
        try:
            self._queue.put(msg)
        except Exception:
            pass

    def _worker(self) -> None:
        device = ''
        try:
            device = str(self._get_device() or '')
        except Exception:
            pass

        if self._mic_apply is None:
            # [补丁] `--settings` 独立设置模式下没有运行中的音频流（电平恒为 0），
            # 硬测只会报「没收到声音」—— 那是在骗用户。直接说清楚。
            # （日志里也记一笔：独立模式与正常运行的面板长得一样，
            #   排查"我点的到底是哪个"时靠这行分辨。）
            logger.info('独立设置模式：没有运行中的音频流，跳过麦克风测试')
            self._post('finish', 0.0, False,
                       '独立设置模式下测不了麦克风（保存后正常启动再试）')
            return

        if self._mic_apply is not None:
            try:
                applied = bool(self._mic_apply(device))
            except Exception as e:
                logger.warning(f'测试麦克风前应用设备失败: {e}')
                applied = False
            if not applied:
                self._post('finish', 0.0, False, '切换设备失败')
                return

        try:
            from core.ui import recording_overlay as RO
        except Exception as e:
            self._post('finish', 0.0, False, f'音频模块不可用: {e}')
            return

        RO.set_test_mode(True)
        peak = 0.0
        try:
            deadline = time.time() + self.DURATION
            while time.time() < deadline:
                time.sleep(0.08)
                level = RO.get_level()
                peak = max(peak, level)
                self._post('level', level)
        finally:
            RO.set_test_mode(False)

        self._post('finish', peak, True)

    def draw_bar(self, level: float) -> None:
        try:
            width = max(0.0, min(1.0, level)) * self.BAR_WIDTH
            self.bar.coords(self.bar_fill, 0, 0, width, self.BAR_HEIGHT)
            color = self._accent
            if level > 0.66:
                color = '#0891b2'
            elif level > 0.33:
                color = '#2563eb'
            self.bar.itemconfigure(self.bar_fill, fill=color)
        except Exception:
            pass

    def finish(self, peak: float, ok: bool, err: str = '') -> None:
        self._testing = False
        try:
            self.btn.configure(state='normal')
        except Exception:
            pass

        if not ok:
            self.verdict.configure(
                text=f'⚠️ 测不了：{err or "音频流没打开"}。'
                     f'可以先跳过，之后在设置里再试。',
                fg='#b91c1c')
            return

        if peak >= LEVEL_OK:
            self.verdict.configure(
                text='✅ 能清楚听到你说话，这个麦克风没问题。', fg='#15803d')
        elif peak >= LEVEL_QUIET:
            self.verdict.configure(
                text='⚠️ 只收到很轻的声音。离麦克风近一点，'
                     '或者去系统设置里把输入音量调大。', fg='#b45309')
        else:
            self.verdict.configure(
                text='❌ 一点声音都没收到。检查三件事：'
                     '① 系统有没有给这个程序的麦克风权限；'
                     '② 是不是选错设备了（换一个再测）；'
                     '③ 麦克风本身是不是被静音了。',
                fg='#b91c1c')
