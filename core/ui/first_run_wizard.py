# coding: utf-8
"""
首次启动向导

装完第一次打开时自动弹出，用 4 步把「能用」这件事做完：
麦克风 → 润色（可选）→ 快捷键用法 → 试一句。

设计取向（这是给非技术用户看的，和设置面板不一样）：
- 每一步只问一件事，术语换成大白话
- **每一步的默认值都已经是对的**，用户可以一路点「下一步」什么都不改
- 润色那一步可以整个跳过（不填 key 也能纯本地用）
- 结束时不显示技术参数，而是"按住 CapsLock 说一句话试试"

产出和设置面板完全一样的 DialogResult，所以复用同一条落盘/生效链路
（core/client/settings_apply.py）。向导只管收集，不碰 Config。
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.ui import settings_schema as S
from .mic_test import MicTestPanel

try:
    from core.ui.toast_constants import DEFAULT_FONT_FAMILY
except Exception:
    DEFAULT_FONT_FAMILY = 'Microsoft YaHei UI'

try:
    from core.ui.dialogs import DialogResult
except Exception:                       # pragma: no cover
    class DialogResult:
        def __init__(self, confirmed: bool = False, **data: Any):
            self.confirmed = confirmed
            self.data = data

        def __bool__(self) -> bool:
            return self.confirmed

        def get(self, key: str, default: Any = None) -> Any:
            return self.data.get(key, default)

try:
    from core.branding import APP_NAME
except Exception:
    APP_NAME = 'Speak More'

F_TITLE = (DEFAULT_FONT_FAMILY, 17, 'bold')
F_STEP = (DEFAULT_FONT_FAMILY, 13, 'bold')
F_BODY = (DEFAULT_FONT_FAMILY, 10)
F_SMALL = (DEFAULT_FONT_FAMILY, 9)
F_HINT = (DEFAULT_FONT_FAMILY, 8)
F_MONO = ('Consolas', 10)

INK = '#1f2937'
MUTED = '#6b7280'
OK = '#15803d'
BAD = '#b91c1c'
ACCENT = '#1d6fd0'

STEP_TITLES = ['欢迎', '选择麦克风', 'AI 润色（可选）', '快捷键用法', '开始使用']

# 「按住说话」用的键，给用户几个常见选择
KEY_CHOICES: List[Tuple[str, str]] = [
    ('CapsLock（大写锁定键）', 'caps_lock'),
    ('右侧 Ctrl', 'ctrl_r'),
    ('右侧 Alt', 'alt_gr'),
    ('F12', 'f12'),
    ('F9', 'f9'),
    ('鼠标侧键 X2', 'x2'),
]


class FirstRunWizard:
    """首次启动向导。见文件头说明。"""

    def __init__(self, parent: Optional[tk.Misc] = None,
                 mic_apply: Optional[Callable[[str], bool]] = None):
        """
        Args:
            parent: 父窗口
            mic_apply: 「测麦克风」前用它把选中的设备真正应用上去
                       （改配置 + 重开音频流）。签名 (设备名) -> 是否成功。
                       由 core/ui/settings_handler.py 注入 —— 向导本身
                       拿不到 app 对象，也刻意不拿（快照必须是纯数据）。
        """
        self._parent = parent
        self._mic_apply = mic_apply
        self.result: Optional[DialogResult] = None
        self._dialog: Optional[tk.Toplevel] = None
        self._step = 0
        self._mic_testing = False

        self._frames: List[tk.Frame] = []
        self._tests: Dict[str, Any] = {}

    # ---------------- 对外 ----------------

    def _new_var(self, kind: str, value: Any = None) -> tk.Variable:
        """
        建 Tk 变量 —— **一律显式绑定 master**，不要图省事用 `tk.BooleanVar()`。

        不传 master 时 Tkinter 会用模块级的 `_default_root`。这个程序里有
        好几个 Tk 根（toast 管理器、录音浮层、上下文对话框、设置面板各一个），
        `_default_root` 指向哪个完全看谁最后建 —— 而根是分线程建的。

        变量一旦挂到**别的解释器**上，勾选框在自己的解释器里读不到它，
        就会被当成"未设置"，渲染成三态的横线：点一下变空白、再点一下才打勾。
        """
        cls = tk.BooleanVar if kind == 'bool' else tk.StringVar
        if value is None:
            return cls(master=self._dialog)
        return cls(master=self._dialog, value=value)

    def show(self, snapshot: Dict[str, Any]) -> DialogResult:
        own_root = None
        if self._parent is None:
            own_root = tk.Tk()
            own_root.withdraw()
            parent: tk.Misc = own_root
        else:
            parent = self._parent

        try:
            self._build(parent, snapshot)
            self._dialog.wait_window()          # type: ignore[union-attr]
        finally:
            # 窗口可能是被外部直接 destroy 的，那样 _close() 不会跑
            self._forget_tk_vars()
            if own_root is not None:
                try:
                    own_root.destroy()
                except Exception:
                    pass
        return self.result or DialogResult(confirmed=False)

    # ---------------- 建窗 ----------------

    def _build(self, parent: tk.Misc, snap: Dict[str, Any]) -> None:
        self.snap = snap
        client = dict(snap.get('client') or {})
        roles = dict(snap.get('llm_roles') or {})
        self.default_role = roles.get('默认') or {}
        self.roles = roles

        dlg = tk.Toplevel(parent)
        self._dialog = dlg
        dlg.title(f'{APP_NAME} 首次设置')
        # 标题栏 + 任务栏 + Alt+Tab 图标（见 core/ui/app_icon.py），
        # 否则这里显示的是 Tk 自带的羽毛，和托盘图标对不上。
        try:
            from .app_icon import apply_to_window as _apply_icon
            _apply_icon(dlg)
        except Exception as _e:
            logger.debug(f'向导图标设置失败: {_e}')
        dlg.withdraw()
        dlg.resizable(False, False)
        dlg.configure(bg='white')

        # ---- 底部按钮条 ----
        # 必须**先** pack 且用 side='bottom'：否则内容高的步骤（LLM 那页）
        # 会把按钮条挤出窗口外面，「下一步」就看不见了 —— 而窗口不可缩放，
        # 用户彻底卡死。钉在底部之后，内容超高只会被裁在内容区里，按钮永远在。
        footer = tk.Frame(dlg, bg='white')
        footer.pack(side='bottom', fill='x', padx=28, pady=(0, 20))

        self._dots = tk.Label(footer, text='', font=F_SMALL, bg='white', fg=MUTED)
        self._dots.pack(side='left')

        self._btn_finish = ttk.Button(footer, text='完成', command=self._on_finish)
        self._btn_next = ttk.Button(footer, text='下一步', command=self._on_next)
        self._btn_back = ttk.Button(footer, text='上一步', command=self._on_back)
        self._btn_skip = ttk.Button(footer, text='跳过设置', command=self._on_skip)

        # ---- 顶部标题 ----
        # 注意：tk.Frame 的内边距只吃标量，元组形式是 pack/grid 的参数
        header = tk.Frame(dlg, bg='white')
        header.pack(side='top', fill='x', padx=28, pady=(22, 0))
        tk.Label(header, text=APP_NAME, font=F_TITLE, bg='white',
                 fg=INK).pack(anchor='w')
        self._step_label = tk.Label(header, text='', font=F_STEP, bg='white',
                                    fg=ACCENT)
        self._step_label.pack(anchor='w', pady=(6, 0))

        # ---- 内容区（吃掉剩余空间）----
        body = tk.Frame(dlg, bg='white')
        body.pack(side='top', fill='both', expand=True, padx=28, pady=18)
        self._body = body

        self._frames = [
            self._step_welcome(body, client),
            self._step_mic(body, client, snap),
            self._step_llm(body, client),
            self._step_shortcut(body, client),
            self._step_done(body, client),
        ]

        dlg.protocol('WM_DELETE_WINDOW', self._on_skip)

        self._show_step(0)
        self._center(dlg, 640, self._measure_height(dlg))

    def _measure_height(self, dlg: tk.Toplevel) -> int:
        """
        把所有步骤量一遍，取最高的那个当窗口高度。

        不这么做的话，每切一步窗口就变一次大小，看着很廉价；
        固定成最高那页又不会有内容被裁掉。
        """
        tallest = 420
        for i, frame in enumerate(self._frames):
            for j, other in enumerate(self._frames):
                if i == j:
                    other.pack(fill='both', expand=True)
                else:
                    other.pack_forget()
            try:
                dlg.update_idletasks()
                tallest = max(tallest, dlg.winfo_reqheight())
            except Exception:
                pass
        return min(tallest, 780)

    # ---------------- 各步骤 ----------------

    def _step_welcome(self, parent: tk.Frame, client: Dict[str, Any]) -> tk.Frame:
        f = tk.Frame(parent, bg='white')
        self._para(f, f'欢迎使用 {APP_NAME}。')
        self._para(f, '按住一个键说话，松开就把文字打到光标位置 —— '
                      '不用切窗口、不用复制粘贴。')
        self._para(f, '识别完全在你的电脑上跑，声音不会上传到任何服务器。')
        self._para(f, '接下来有 3 个小问题（都能跳过），大概 30 秒。',
                   color=MUTED, font=F_SMALL)
        return f

    def _step_mic(self, parent: tk.Frame, client: Dict[str, Any],
                  snap: Dict[str, Any]) -> tk.Frame:
        f = tk.Frame(parent, bg='white')
        self._para(f, '你想用哪个麦克风？')
        self._para(f, '用默认的就行，之后在设置里随时能改。', color=MUTED, font=F_SMALL)

        row = tk.Frame(f, bg='white')
        row.pack(fill='x', pady=(14, 6))

        choices = list(snap.get('devices') or [('系统默认（跟随系统设置）', '')])
        labels = [c[0] for c in choices]
        self._dev_map = {c[0]: c[1] for c in choices}

        var = self._new_var('str')
        current = str(client.get('mic_device', '') or '')
        shown = next((lbl for lbl, val in self._dev_map.items() if val == current),
                     labels[0])
        var.set(shown)

        combo = ttk.Combobox(row, textvariable=var, values=labels, width=44,
                             state='readonly')
        combo.pack(side='left')
        self._tests['mic_var'] = var
        self._tests['mic_combo'] = combo

        ttk.Button(row, text='刷新', width=6,
                   command=lambda: self._refresh_devices()).pack(side='left', padx=6)

        self._mic_note = tk.Label(f, text='', font=F_SMALL, bg='white', fg=MUTED,
                                  wraplength=540, justify='left')
        self._mic_note.pack(anchor='w', pady=(6, 0))
        self._update_mic_note()

        # ---- 试一下 ----
        # 复用 core/ui/mic_test.py 的共用组件（设置面板里也是同一个）
        self._para(f, '光看名字不一定认得出来。点下面按钮说句话，看看能不能收到：',
                   color=MUTED, font=F_SMALL)

        self._mic_test = MicTestPanel(
            f,
            get_device=lambda: self._dev_map.get(self._tests['mic_var'].get(), ''),
            mic_apply=self._mic_apply,
            bg='white', font=F_SMALL, accent=ACCENT)
        self._mic_test.pack(fill='x', pady=(2, 6))
        return f

    def _step_llm(self, parent: tk.Frame, client: Dict[str, Any]) -> tk.Frame:
        f = tk.Frame(parent, bg='white')
        self._para(f, f'{APP_NAME} 已经能认字了。要不要再让它「说人话」？')
        self._para(f, '开启后，识别出的文字会先交给大模型润色一遍 —— '
                      '去掉"呃、那个"、补上标点、修正错别字 —— 再打到你的光标处。',
                   color=MUTED, font=F_SMALL)
        self._para(f, '这一步完全可以跳过：不填 Key 也能用，只是文字会比较口语。',
                   color=MUTED, font=F_SMALL)

        enable_var = self._new_var('bool',
                                   bool(self.default_role.get('enabled')))
        ttk.Checkbutton(f, text='开启 AI 润色', variable=enable_var).pack(
            anchor='w', pady=(14, 10))
        self._tests['llm_enable'] = enable_var

        grid = tk.Frame(f, bg='white')
        grid.pack(fill='x')
        grid.columnconfigure(1, weight=1)

        # 只有三项：Base URL / API Key / 模型。
        # 不摆"服务商"下拉 —— 有了 Base URL，它就只剩"决定走哪套 SDK"
        # 这一个作用，而我们统一走 OpenAI 兼容接口，DeepSeek / 智谱 /
        # 本地代理全都覆盖得到。
        row = 0

        tk.Label(grid, text='Base URL', font=F_BODY, bg='white',
                 fg=INK).grid(row=row, column=0, sticky='w', pady=5, padx=(0, 12))
        url_var = self._new_var(
            'str', str(self.default_role.get('api_url')
                       or S.DEFAULT_LLM_BASE_URL))
        ttk.Entry(grid, textvariable=url_var, width=46,
                  font=F_MONO).grid(row=row, column=1, sticky='ew', pady=5)
        self._tests['url_var'] = url_var
        row += 1

        tk.Label(grid, text='API Key', font=F_BODY, bg='white',
                 fg=INK).grid(row=row, column=0, sticky='w', pady=5, padx=(0, 12))
        # 作者留的脱敏假 key（一串 *）要当空处理，否则用户会以为已经配好了
        saved_key = str(self.default_role.get('api_key') or '')
        if S.is_placeholder_secret(saved_key):
            saved_key = ''
        key_var = self._new_var('str', saved_key)
        key_entry = ttk.Entry(grid, textvariable=key_var, show='*', width=44,
                              font=F_MONO)
        key_entry.grid(row=row, column=1, sticky='ew', pady=5)
        self._tests['key_var'] = key_var
        self._tests['key_entry'] = key_entry
        row += 1

        tk.Label(grid, text='模型', font=F_BODY, bg='white',
                 fg=INK).grid(row=row, column=0, sticky='w', pady=5, padx=(0, 12))
        model_var = self._new_var(
            'str', str(self.default_role.get('model') or 'deepseek-chat'))
        # 纯文本框，不给下拉框 —— 模型名各家各版本都不一样，下拉框只会让
        # 用户以为"必须从这里面挑"。常见的几种写在下面的提示行里。
        ttk.Entry(grid, textvariable=model_var, width=46,
                  font=F_MONO).grid(row=row, column=1, sticky='ew', pady=5)
        self._tests['model_var'] = model_var

        tk.Label(grid, text='填服务商文档里的模型名，常见的有：'
                            + '、'.join(S.LLM_MODELS),
                 font=F_HINT, bg='white', fg=MUTED, wraplength=460,
                 justify='left').grid(row=row + 1, column=1, sticky='w',
                                      pady=(0, 4))

        tk.Label(f, text='地址要带 /v1。' + S.base_url_hint(),
                 font=F_HINT, bg='white', fg=MUTED, wraplength=540,
                 justify='left').pack(anchor='w', pady=(6, 0))

        # 测试连接
        test_row = tk.Frame(f, bg='white')
        test_row.pack(fill='x', pady=(12, 0))
        self._btn_test = ttk.Button(test_row, text='测试连接', width=12,
                                    command=self._on_test)
        self._btn_test.pack(side='left')
        self._test_label = tk.Label(test_row, text='', font=F_SMALL, bg='white',
                                    fg=MUTED, wraplength=400, justify='left')
        self._test_label.pack(side='left', padx=10)
        return f

    def _step_shortcut(self, parent: tk.Frame, client: Dict[str, Any]) -> tk.Frame:
        f = tk.Frame(parent, bg='white')
        self._para(f, '想怎么触发？')

        mode_var = self._new_var('str')
        hold = self._current_hold_mode(client)
        mode_var.set('hold' if hold else 'toggle')

        ttk.Radiobutton(f, text='按住说话，松开就上屏　　　（默认，适合一两句话）',
                        variable=mode_var, value='hold').pack(anchor='w', pady=(14, 4))
        ttk.Radiobutton(f, text='点一下开始持续聆听，再点一下结束　（不用一直按着，适合长段口述）',
                        variable=mode_var, value='toggle').pack(anchor='w', pady=4)
        self._tests['mode_var'] = mode_var

        key_row = tk.Frame(f, bg='white')
        key_row.pack(fill='x', pady=(18, 0))
        tk.Label(key_row, text='用哪个键', font=F_BODY, bg='white',
                 fg=INK).pack(side='left', padx=(0, 12))

        key_labels = [k[0] for k in KEY_CHOICES]
        key_map = {k[0]: k[1] for k in KEY_CHOICES}
        cur_key = self._current_primary_key(client)
        key_var = self._new_var(
            'str', next((l for l, v in key_map.items() if v == cur_key),
                        key_labels[0]))
        ttk.Combobox(key_row, textvariable=key_var, values=key_labels, width=28,
                     state='readonly').pack(side='left')
        self._key_map = key_map
        self._tests['key_var_shortcut'] = key_var

        self._para(f, 'CapsLock 最顺手：短按一下它仍然照常切换大小写，不影响你原来的习惯。',
                   color=MUTED, font=F_SMALL)
        return f

    def _step_done(self, parent: tk.Frame, client: Dict[str, Any]) -> tk.Frame:
        f = tk.Frame(parent, bg='white')
        self._done_title = self._para(f, '准备好了。')
        self._done_body = tk.Label(f, text='', font=F_BODY, bg='white', fg=INK,
                                   justify='left', wraplength=540)
        self._done_body.pack(anchor='w', pady=(10, 0))

        self._para(f, '配置随时可以在托盘图标上点左键修改。', color=MUTED,
                   font=F_SMALL)

        tip = tk.Frame(f, bg='#eff6ff')
        tip.pack(fill='x', pady=(18, 0), ipady=12, ipadx=14)
        self._tip_label = tk.Label(tip, text='', font=F_BODY, bg='#eff6ff',
                                   fg=ACCENT, justify='left', wraplength=500)
        self._tip_label.pack(anchor='w')
        return f

    # ---------------- 小工具 ----------------

    @staticmethod
    def _para(parent: tk.Frame, text: str, color: str = INK,
              font: Any = F_BODY) -> tk.Label:
        lbl = tk.Label(parent, text=text, font=font, bg='white', fg=color,
                       justify='left', wraplength=540, anchor='w')
        lbl.pack(anchor='w', pady=(0, 8))
        return lbl

    @staticmethod
    def _current_hold_mode(client: Dict[str, Any]) -> bool:
        for sc in client.get('shortcuts') or []:
            if isinstance(sc, dict) and sc.get('enabled'):
                return bool(sc.get('hold_mode', True))
        return True

    @staticmethod
    def _current_primary_key(client: Dict[str, Any]) -> str:
        for sc in client.get('shortcuts') or []:
            if isinstance(sc, dict) and sc.get('enabled'):
                return str(sc.get('key', 'caps_lock'))
        return 'caps_lock'

    def _update_mic_note(self) -> None:
        try:
            from core.client.audio.device_select import describe_current
            text = describe_current()
        except Exception:
            text = '（无法读取当前麦克风信息，不影响使用）'
        if getattr(self, '_mic_note', None) is not None:
            self._mic_note.configure(text=text)

    def _refresh_devices(self) -> None:
        try:
            from core.client.audio.device_select import list_input_devices
            devices = [('系统默认（跟随系统设置）', '')] + [
                (name, name) for _i, name, _c in list_input_devices()
            ]
        except Exception:
            return
        self._dev_map = {d[0]: d[1] for d in devices}
        combo = self._tests.get('mic_combo')
        if isinstance(combo, ttk.Combobox):
            combo.configure(values=[d[0] for d in devices])
        self._update_mic_note()

    # ---------------- 步骤切换 ----------------

    def _show_step(self, index: int) -> None:
        self._step = max(0, min(index, len(self._frames) - 1))
        for i, frame in enumerate(self._frames):
            if i == self._step:
                frame.pack(fill='both', expand=True)
            else:
                frame.pack_forget()

        self._step_label.configure(
            text=f'{self._step + 1}. {STEP_TITLES[self._step]}'
                 if self._step > 0 else STEP_TITLES[self._step])
        self._dots.configure(
            text='●' * (self._step + 1) + '○' * (len(self._frames) - self._step - 1))

        last = self._step == len(self._frames) - 1

        for btn in (self._btn_finish, self._btn_next, self._btn_back, self._btn_skip):
            btn.pack_forget()

        if last:
            self._btn_finish.pack(side='right', padx=(6, 0))
        else:
            self._btn_next.pack(side='right', padx=(6, 0))
            self._btn_skip.pack(side='left', padx=(14, 0))
        if self._step > 0:
            self._btn_back.pack(side='right', padx=(6, 0))

        if last:
            self._refresh_done()

    def _on_next(self) -> None:
        self._show_step(self._step + 1)

    def _on_back(self) -> None:
        self._show_step(self._step - 1)

    def _on_skip(self) -> None:
        # 跳过 = 什么都不改，只标记向导已看过
        self.result = DialogResult(confirmed=True, wizard_done=True)
        self._close()

    def _on_finish(self) -> None:
        self.result = DialogResult(confirmed=True, **self._collect(),
                                   wizard_done=True)
        self._close()

    # ---------------- 测试连接 ----------------

    def _on_test(self) -> None:
        key = self._tests['key_var'].get().strip()
        model = self._tests['model_var'].get().strip()
        url = self._tests['url_var'].get().strip()

        self._btn_test.configure(state='disabled')
        self._test_label.configure(text='正在测试…', fg=MUTED)

        def worker() -> None:
            try:
                from core.client.llm.llm_test import test_connection
                # 统一走 OpenAI 兼容接口，地址由用户填的 Base URL 决定
                ok, msg = test_connection(S.GENERIC_PROVIDER, url, key, model)
            except Exception as e:
                ok, msg = False, f'测试出错: {e}'

            def apply() -> None:
                try:
                    self._test_label.configure(text=msg, fg=OK if ok else BAD)
                    self._btn_test.configure(state='normal')
                except Exception:
                    pass

            try:
                if self._dialog is not None:
                    self._dialog.after(0, apply)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True, name='LLMTest').start()

    # ---------------- 收集 ----------------

    def _refresh_done(self) -> None:
        """完成页：用大白话总结一遍，不出现技术参数。"""
        mode = self._tests['mode_var'].get()
        key_label = self._tests['key_var_shortcut'].get()
        key_name = key_label.split('（')[0].strip()

        lines = []
        if mode == 'hold':
            lines.append(f'· 按住 {key_name} 说话，松开就自动打到光标处')
        else:
            lines.append(f'· 点一下 {key_name} 开始听，再点一下结束并上屏')

        if self._tests['llm_enable'].get():
            lines.append('· 开启 AI 润色：说的话会先被整理通顺再上屏')
        else:
            lines.append('· 纯本地识别：不联网、不花一分钱')

        self._done_body.configure(text='\n'.join(lines))

        # 提示文案必须跟着**模式和按键**走：
        # 之前这里写死了「按住 xxx 说一句话，然后松开」，用户选了
        # 「点一下持续聆听」也照样提示长按，自相矛盾。
        if mode == 'hold':
            tip = f'现在就可以试试：按住 {key_name} 说一句话，然后松开。'
        else:
            tip = (f'现在就可以试试：点一下 {key_name} 开始，说几句话，'
                   f'再点一下结束。屏幕下方能看到波形。')
        self._tip_label.configure(text=tip)

    def _collect(self) -> Dict[str, Any]:
        snap_client = dict(self.snap.get('client') or {})

        # ---- 麦克风 ----
        snap_client['mic_device'] = self._dev_map.get(
            self._tests['mic_var'].get(), '')

        # ---- 快捷键：改所有已启用项的 hold_mode，改第一项的主键 ----
        key = self._key_map.get(self._tests['key_var_shortcut'].get(), 'caps_lock')
        hold = self._tests['mode_var'].get() == 'hold'
        shortcuts = []
        first = True
        for sc in snap_client.get('shortcuts') or []:
            if not isinstance(sc, dict):
                continue
            item = dict(sc)
            item['hold_mode'] = hold
            if first and item.get('enabled'):
                item['key'] = key
                # 鼠标键换键盘键时类型要跟着改，否则监听器挂不上
                item['type'] = 'mouse' if key.startswith('x') else 'keyboard'
                first = False
            shortcuts.append(item)
        if shortcuts:
            snap_client['shortcuts'] = shortcuts

        # ---- LLM ----
        enabled = bool(self._tests['llm_enable'].get())
        snap_client['llm_enabled'] = enabled

        role = dict(self.default_role)

        # Base URL 是唯一的连接信息，空着一定是错的（会退回服务商官方地址）
        url = self._tests['url_var'].get().strip() or S.DEFAULT_LLM_BASE_URL
        role['api_url'] = url
        role['api_key'] = self._tests['key_var'].get().strip()
        role['model'] = self._tests['model_var'].get().strip() or 'deepseek-chat'
        # 总开关和角色开关联动：角色检测器要求 role_config.enabled 为 True
        # 才会真的走润色，只开一个会"看着开了但没效果"
        role['enabled'] = enabled

        roles = {name: dict(vals) for name, vals in self.roles.items()}
        roles['默认'] = role

        return {'client': snap_client, 'llm_roles': roles}

    # ---------------- 杂项 ----------------

    def _close(self) -> None:
        dlg = self._dialog
        self._dialog = None
        if dlg is not None:
            try:
                dlg.destroy()
            except Exception:
                pass
        self._forget_tk_vars()

    def _forget_tk_vars(self) -> None:
        """
        断开所有 Tk 变量到 Tcl 解释器的引用。

        变量绑在**别的线程**的 Tk 解释器上（settings_handler 的
        SettingsDialogThread）。留到解释器退出时由主线程 GC 去跑
        Variable.__del__，会去调那个正持锁跑 mainloop 的解释器 —— 程序就
        永久卡死、退不出去。设置面板的 _forget_tk_vars 里有完整的踩坑栈。
        """
        for item in list(self._tests.values()):
            try:
                if getattr(item, '_tk', None) is not None:
                    item._tk = None
            except Exception:
                pass
        self._tests.clear()

    @staticmethod
    def _center(win: tk.Toplevel, width: int, height: int) -> None:
        try:
            win.update_idletasks()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            width, height = min(width, sw - 40), min(height, sh - 80)
            x, y = max(0, (sw - width) // 2), max(0, (sh - height) // 2 - 40)
            win.geometry(f'{width}x{height}+{x}+{y}')
            win.deiconify()
            win.lift()
            win.focus_force()
        except Exception:
            try:
                win.deiconify()
            except Exception:
                pass


def show_first_run_wizard(snapshot: Dict[str, Any],
                          parent: Optional[tk.Misc] = None,
                          mic_apply: Optional[Callable[[str], bool]] = None) -> DialogResult:
    return FirstRunWizard(parent, mic_apply).show(snapshot)
