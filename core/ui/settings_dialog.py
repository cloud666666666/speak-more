# coding: utf-8
"""
设置面板的纯 UI 层

职责边界（重要）：
    本文件**只吃 dict、只吐 DialogResult**，完全不知道 Config / app 的存在。
    所有跟作者代码打交道的事都收在：
      - core/ui/settings_handler.py   （线程模型 + 快照 + 保存编排）
      - core/client/settings_apply.py （热生效）
    这样作者升级要改的只有那两个文件。

两个刻意的设计：
  1. **不做模态**。没有 grab_set() / transient()。用户要能一边开着设置
     一边对照文档、一边操作桌面。core/ui/dialogs.py 的 create_modal_dialog()
     在这里不能用。
  2. 每页可滚动。字段多的页签（输出行为、高级）会超出屏幕高度。

输入输出契约：
    show(snapshot) -> DialogResult
    snapshot = {
        'client':       {'<ClientConfig 字段>': 当前值, ...},
        'llm_roles':    {'默认': {'api_key': ..., 'model': ...}, ...},
        'role_names':   ['默认', ...],
        'current_role': '默认',
        'devices':      [(显示名, 设备名), ...],   # 麦克风候选，首项是「系统默认」
        'meta':         '补丁 v0.1.0 · ...',        # 底部状态栏
        'base_version': '2.6',
    }
    DialogResult.data = {
        'client':    {'<被改过的字段>': 新值, ...},
        'llm_roles': {'<角色名>': {'<被改过的字段>': 新值}},
        'reset':     True/不存在,
    }
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.ui import settings_schema as S
from . import key_capture as kc
from . import logger
from .mic_test import MicTestPanel

# 安装目录（core/ui/settings_dialog.py -> 往上三级）
# 程序根目录（打包时 = exe 同级目录）。别用 __file__ 上溯：打包时 core/ 可能是
# 目录联接，resolve() 会展开成源码树路径，导致「打开日志/安装目录」打开错地方。
from core.tools.paths import install_root as _install_root
REPO_ROOT = _install_root()

try:                                    # 软依赖：拿不到就用兜底值
    from core.ui.toast_constants import DEFAULT_FONT_FAMILY
except Exception:
    DEFAULT_FONT_FAMILY = 'Microsoft YaHei UI'

try:
    from core.ui.dialogs import DialogResult
except Exception:                       # 兜底，保证本模块可独立 import
    class DialogResult:
        def __init__(self, confirmed: bool = False, **data: Any):
            self.confirmed = confirmed
            self.data = data

        def __bool__(self) -> bool:
            return self.confirmed

        def get(self, key: str, default: Any = None) -> Any:
            return self.data.get(key, default)

try:
    from core.branding import SETTINGS_TITLE
except Exception:
    SETTINGS_TITLE = '设置'


F_NORMAL = (DEFAULT_FONT_FAMILY, 10)
F_SMALL = (DEFAULT_FONT_FAMILY, 9)
F_HINT = (DEFAULT_FONT_FAMILY, 8)
F_TITLE = (DEFAULT_FONT_FAMILY, 11, 'bold')
F_GROUP = (DEFAULT_FONT_FAMILY, 10, 'bold')     # 页内分组的小标题
F_MONO = ('Consolas', 10)

INK = '#1f2937'                 # 正文/小标题
HINT_COLOR = '#8a8a8a'
WARN_COLOR = '#c07000'

# 快捷键的两种触发方式。
# 用下拉框而不是勾选框：勾选框表达"按住 / 不按住"两个选项很别扭
# （用户会问"不勾是什么意思"），而且之前旁边还有「阻塞」「启用」两个勾，
# 三个勾并排完全看不懂。
MODE_HOLD = '按住说话，松开上屏'
MODE_TOGGLE = '点一下开始 / 再点一下结束'
_SHORTCUT_MODES = (MODE_HOLD, MODE_TOGGLE)

# 鼠标侧键
_MOUSE_KEYS = frozenset({'x1', 'x2'})


def _key_to_type(key: str) -> str:
    """按键名推导输入类型 —— 用户不需要手选，看名字就知道。"""
    return 'mouse' if key.strip().lower() in _MOUSE_KEYS else 'keyboard'


# ============================================================
#  可滚动容器
# ============================================================

class _ScrollFrame(ttk.Frame):
    """一个内部可滚动的 Frame。把控件加到 .body 上。"""

    def __init__(self, master: tk.Misc, **kw: Any):
        super().__init__(master, **kw)
        self._canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        sb = ttk.Scrollbar(self, orient='vertical', command=self._canvas.yview)
        self.body = ttk.Frame(self._canvas)

        self._win = self._canvas.create_window((0, 0), window=self.body, anchor='nw')
        self._canvas.configure(yscrollcommand=sb.set)
        self._canvas.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

        self.body.bind('<Configure>',
                       lambda _e: self._canvas.configure(
                           scrollregion=self._canvas.bbox('all')))
        self._canvas.bind('<Configure>',
                          lambda e: self._canvas.itemconfigure(self._win, width=e.width))
        # 鼠标在面板上时滚轮才生效，避免抢走别的窗口的滚轮
        self._canvas.bind('<Enter>', lambda _e: self._canvas.bind_all(
            '<MouseWheel>', self._on_wheel))
        self._canvas.bind('<Leave>', lambda _e: self._canvas.unbind_all('<MouseWheel>'))

    def _on_wheel(self, event: Any) -> None:
        if self._canvas.yview() != (0.0, 1.0):
            self._canvas.yview_scroll(int(-event.delta / 120), 'units')


# ============================================================
#  主对话框
# ============================================================

class SettingsDialog:
    """设置面板。见文件头的输入输出契约。"""

    def __init__(self, parent: Optional[tk.Misc] = None,
                 mic_apply: Optional[Callable[[str], bool]] = None,
                 key_capture: Optional[Callable[..., Any]] = None,
                 insert_text: Optional[Callable[[str], None]] = None):
        """
        Args:
            parent: 父窗口
            mic_apply: 「测试麦克风」前用它把选中的设备真正应用上去
                       （改配置 + 重开音频流）。签名 (设备名) -> 是否成功。
                       由 core/ui/settings_handler.py 注入 —— 本模块是纯 UI，
                       拿不到 app 对象，也刻意不拿。
            key_capture: 「按一下键就捕获」。签名 (timeout, cancel) -> CaptureResult。
                       同样由 settings_handler 注入：捕获时必须先把宿主 app 的
                       快捷键监听停掉（否则 CapsLock 这类会被应用自己吞掉），
                       而那需要 app 对象。见 core/ui/key_capture.py。
        """
        self._parent = parent
        self._mic_apply = mic_apply
        self._key_capture = key_capture
        self._insert_text = insert_text
        self.result: Optional[DialogResult] = None
        self._dialog: Optional[tk.Toplevel] = None

        self._vars: Dict[str, tk.Variable] = {}
        self._widgets: Dict[str, tk.Widget] = {}
        self._choice_maps: Dict[str, Dict[str, str]] = {}
        self._multiline: Dict[str, tk.Text] = {}
        self._shortcut_rows: List[Dict[str, Any]] = []
        self._shortcut_holder: Optional[ttk.Frame] = None
        # 按键捕获的状态（捕获跑在后台线程，Tk 这边轮询结果）
        self._shortcut_status: Optional[ttk.Label] = None
        self._capturing_row: Optional[Dict[str, Any]] = None
        self._capture_cancel: Optional[threading.Event] = None
        self._capture_queue: Optional[queue.Queue] = None

        # 角色相关：LLM 页签顶部的下拉 + 每个角色的值缓存
        self._role_names: List[str] = []
        self._current_role: str = ''
        self._role_cache: Dict[str, Dict[str, Any]] = {}
        self._role_var: Optional[tk.StringVar] = None
        self._device_choices: List[Tuple[str, str]] = []

        # 「热词」页的状态。_hot_manual 为 None 表示这一页没建出来 ——
        # _collect_hotwords() 靠它判断"要不要动磁盘上的 hot.txt"。
        self._hot_manual: Optional[tk.Text] = None
        self._hot_lint: Optional[ttk.Label] = None

    # ---------------- 对外入口 ----------------

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
            # 必须在这里也清一次：窗口有可能是被外部直接 destroy 的
            # （比如管理器那边统一收尾），那样 _close() 不会跑。
            self._forget_tk_vars()
            if own_root is not None:
                try:
                    own_root.destroy()
                except Exception:
                    pass

        return self.result or DialogResult(confirmed=False)

    # ---------------- 建窗 ----------------

    def _build(self, parent: tk.Misc, snap: Dict[str, Any]) -> None:
        snapshot = self._normalize(snap)

        dlg = tk.Toplevel(parent)
        self._dialog = dlg
        dlg.title(SETTINGS_TITLE)
        # 标题栏 + 任务栏 + Alt+Tab 三处图标一次设好（见 core/ui/app_icon.py）。
        # 放在 withdraw() 之前也没问题：设的是窗口属性，deiconify 时自然带上。
        try:
            from .app_icon import apply_to_window as _apply_icon
            _apply_icon(dlg)
        except Exception as _e:
            logger.debug(f'设置面板图标设置失败: {_e}')
        dlg.withdraw()
        dlg.resizable(True, True)
        dlg.minsize(760, 540)
        # 刻意不调 dlg.transient() / dlg.grab_set()：见文件头说明

        outer = ttk.Frame(dlg, padding=10)
        outer.pack(fill='both', expand=True)

        # ---- 底部按钮条 ----
        #
        # **必须最先 pack，而且用 side='bottom'** —— 这是踩过的大坑：
        # Notebook 的"请求高度"等于最高那一页的内容高度（「高级」页有 26 项、
        # 一千多像素），先 pack 它的话会把窗口塞满，后面才 pack 的按钮条
        # 只能分到 **1 像素** —— 「保存」和「取消」直接看不见。
        #
        # 表现极具迷惑性：面板用着完全正常，只是用户改完设置**找不到保存**，
        # 于是所有改动都不生效，还以为是功能坏了。
        status = ttk.Frame(outer)
        status.pack(side='bottom', fill='x', pady=(8, 0))

        # **按钮先 pack，状态文字后 pack**。
        # pack 是按顺序分配宽度的：文字先占的话，长文字会把按钮挤成 0 宽 ——
        # 踩过：状态栏那行 85 个字符把「保存」挤到 **1 像素**，
        # 看得见字样但点不到，用户改完设置根本存不进去。
        btns = ttk.Frame(status)
        btns.pack(side='right')
        for text, cmd in (('取消', self._on_cancel),
                          ('保存', self._on_confirm)):
            ttk.Button(btns, text=text, command=cmd).pack(side='left', padx=4)

        meta = ttk.Label(status, text=str(snapshot.get('meta', '')), font=F_HINT,
                         foreground=HINT_COLOR, anchor='w')
        meta.pack(side='left', fill='x', expand=True)

        # 「恢复默认」搬到「关于」页去了 —— 它跟保存/取消不是一类操作，
        # 并排摆着的时候被误点过好几次，每次都会把 config_local.json
        # 整个改名备份掉，用户改了半天的设置就这么没了。

        # ---- 内容区（吃掉剩余空间）----
        # 面板分三页：常用 / 高级 / 关于。
        # 之前是 7 个页签、35 个配置项平铺 —— 那不是"开箱即用"，
        # 那是"给你一堆开关自己研究"。功能一个没删，只是把
        # 有合理默认值、基本不用动的收进了「高级」。
        nb = ttk.Notebook(outer)
        nb.pack(side='top', fill='both', expand=True)
        # [补丁] 切回「热词」页时重读一遍 hot.txt —— 「记录」页的「纠正」会把学到的
        # 词直接写进文件（不经本面板保存），不重读的话这一页显示的还是打开时的旧内容。
        nb.bind('<<NotebookTabChanged>>', self._on_tab_changed)

        for tier, tab_title in S.TIERS:
            if tier == 'about':
                page = _ScrollFrame(nb)
                nb.add(page, text=tab_title)
                self._build_about(page.body)
                continue

            if tier == 'hotwords':
                # 「热词」页没有字段表 —— 自动学到的词要能逐条增删改，
                # 声明式字段表表达不了这种结构，所以自己画。
                page = _ScrollFrame(nb)
                nb.add(page, text=tab_title)
                self._build_hotwords(page.body, snapshot)
                continue

            if tier == 'records':
                # 「记录」页：上面是历史列表（自己画，同「热词」页的理由），
                # 下面是保留策略（声明式字段，照常渲染）。
                page = ttk.Frame(nb)
                nb.add(page, text=tab_title)
                self._build_records(page, snapshot)
                continue

            groups = S.groups_in_tier(tier)
            if not groups:
                continue

            page = _ScrollFrame(nb)
            nb.add(page, text=tab_title)
            row = 0
            for gi, group in enumerate(groups):
                if gi > 0:
                    ttk.Separator(page.body, orient='horizontal').grid(
                        row=row, column=0, columnspan=3, sticky='ew',
                        padx=8, pady=(18, 0))
                    row += 1
                row = self._build_group(page.body, group, snapshot, row)

        dlg.protocol('WM_DELETE_WINDOW', self._on_cancel)
        # Esc 走 _on_escape：正在捕获时它取消**捕获**，而不是关掉整个面板
        # （用户想取消捕获反而丢了改了一半的设置，是最招人烦的失败模式）
        dlg.bind('<Escape>', self._on_escape)

        self._center(dlg, 840, 660)

    def _normalize(self, snap: Dict[str, Any]) -> Dict[str, Any]:
        """把外部快照整理成本对话框内部用的形式，补齐缺省。"""
        client = dict(snap.get('client') or {})
        roles = snap.get('llm_roles') or {}
        self._role_names = list(snap.get('role_names') or list(roles) or ['默认'])
        self._current_role = str(snap.get('current_role') or self._role_names[0])
        self._role_cache = {name: dict(vals or {}) for name, vals in roles.items()}
        for name in self._role_names:
            self._role_cache.setdefault(name, {})
        self._device_choices = list(snap.get('devices') or [('系统默认', '')])
        return {'client': client, **snap}

    def _build_group(self, body: ttk.Frame, group: S.Group,
                     snap: Dict[str, Any], start_row: int = 0) -> int:
        body.columnconfigure(1, weight=1)
        row = start_row

        # 组标题。一个页里有好几组，靠它分段。
        ttk.Label(body, text=group.title, font=F_GROUP,
                  foreground=INK).grid(
            row=row, column=0, columnspan=3, sticky='w',
            padx=8, pady=(14, 2))
        row += 1

        if group.note:
            ttk.Label(body, text=group.note, font=F_HINT, foreground=HINT_COLOR,
                      wraplength=740, justify='left').grid(
                row=row, column=0, columnspan=3, sticky='w',
                padx=8, pady=(0, 10))
            row += 1

        basic = [f for f in group.fields if f.section == 'basic']
        advanced = [f for f in group.fields if f.section == 'advanced']

        for f in basic:
            row += self._build_field(body, f, snap, row)

        # LLM 组紧跟在连接信息后面放一个「测试连接」
        if group.id == 'llm':
            row += self._build_llm_test(body, row)

        # 麦克风组：选完设备直接能测，不用先保存再回来试
        if group.id == 'mic':
            row += self._build_mic_test(body, row)

        if advanced:
            holder = ttk.LabelFrame(body, text=' 更多 ', padding=(8, 4))
            holder.grid(row=row, column=0, columnspan=3, sticky='ew',
                        padx=8, pady=(10, 8))
            holder.columnconfigure(1, weight=1)
            arow = 0
            for f in advanced:
                arow += self._build_field(holder, f, snap, arow)
            row += 1

        return row

    def _build_about(self, body: ttk.Frame) -> None:
        """
        「关于」页：版本、日志、以及那些"出问题时才用得上"的按钮。

        恢复默认放在这里，是因为它跟保存/取消不是一类操作 ——
        之前并排摆着，用户误点过好几次，把改好的配置整个重置掉。
        """
        body.columnconfigure(0, weight=1)
        row = 0

        try:
            from core.branding import APP_NAME, UPSTREAM_NAME, UPSTREAM_URL
        except Exception:
            APP_NAME, UPSTREAM_NAME, UPSTREAM_URL = 'Speak More', '', ''

        try:
            import config_client
            version = getattr(config_client, '__version__', '?')
        except Exception:
            version = '?'

        ttk.Label(body, text=APP_NAME, font=F_TITLE, foreground=INK).grid(
            row=row, column=0, columnspan=3, sticky='w', padx=8, pady=(14, 2))
        row += 1
        ttk.Label(body, text=f'内核 {version}　（基于 {UPSTREAM_NAME} 二次开发）',
                  font=F_SMALL, foreground=HINT_COLOR).grid(
            row=row, column=0, columnspan=3, sticky='w', padx=8, pady=(0, 6))
        row += 1

        try:
            from config_local import status_summary
            status = status_summary()
        except Exception:
            status = ''
        if status:
            ttk.Label(body, text=status, font=F_HINT, foreground=HINT_COLOR,
                      wraplength=740, justify='left').grid(
                row=row, column=0, columnspan=3, sticky='w',
                padx=8, pady=(0, 14))
            row += 1

        # [补丁] 显卡与推理后端：服务端启动时探测一次，写进 logs/gpu_probe.json，
        # 这里读出来展示 —— 用户想知道"到底有没有用上显卡"时不用去翻日志。
        gpu_line = self._gpu_summary()
        if gpu_line:
            ttk.Label(body, text=gpu_line, font=F_HINT, foreground=HINT_COLOR,
                      wraplength=740, justify='left').grid(
                row=row, column=0, columnspan=3, sticky='w',
                padx=8, pady=(0, 14))
            row += 1

        # [补丁] 自检：模型与推理库齐不齐（现场检查，所以永远是最新的）
        model_text, model_ok = self._model_summary()
        if model_text:
            ttk.Label(body, text=model_text, font=F_HINT,
                      foreground=HINT_COLOR if model_ok else WARN_COLOR,
                      wraplength=740, justify='left').grid(
                row=row, column=0, columnspan=3, sticky='w',
                padx=8, pady=(0, 14))
            row += 1

        # [补丁] 开机自启 —— README 里一直承诺"设置面板里一键开关"，
        # 但 autostart.py 之前没有任何调用方（虚标）。这里把它接上。
        ttk.Separator(body, orient='horizontal').grid(
            row=row, column=0, columnspan=3, sticky='ew', padx=8, pady=(4, 12))
        row += 1

        ttk.Label(body, text='开机自启', font=F_GROUP, foreground=INK).grid(
            row=row, column=0, columnspan=3, sticky='w', padx=8, pady=(0, 6))
        row += 1
        self._autostart_btn = ttk.Button(body, text='开启开机自启',
                                         command=self._toggle_autostart)
        self._autostart_btn.grid(row=row, column=0, sticky='w', padx=8,
                                 pady=(0, 6))
        row += 1
        ttk.Label(body,
                  text='在「启动」文件夹里放一个带图标的快捷方式，开机自动运行。'
                       '想取消就点上面的按钮，或直接把那个快捷方式删掉。',
                  font=F_HINT, foreground=HINT_COLOR, wraplength=740,
                  justify='left').grid(row=row, column=0, columnspan=3,
                                       sticky='w', padx=8, pady=(0, 14))
        row += 1
        self._refresh_autostart()

        ttk.Separator(body, orient='horizontal').grid(
            row=row, column=0, columnspan=3, sticky='ew', padx=8, pady=(4, 12))
        row += 1

        ttk.Label(body, text='出问题时', font=F_GROUP, foreground=INK).grid(
            row=row, column=0, columnspan=3, sticky='w', padx=8, pady=(0, 6))
        row += 1

        btns = ttk.Frame(body)
        btns.grid(row=row, column=0, columnspan=3, sticky='w', padx=8, pady=(0, 8))
        row += 1

        ttk.Button(btns, text='打开日志文件夹', command=self._open_logs).pack(
            side='left', padx=(0, 8))
        ttk.Button(btns, text='打开安装目录', command=self._open_install_dir).pack(
            side='left', padx=(0, 8))

        ttk.Label(body,
                  text='出问题时的排查顺序：先看 logs\\client_latest.log 的最后几十行；'
                       '那里记着每次保存提交了什么、有没有报错。',
                  font=F_HINT, foreground=HINT_COLOR, wraplength=740,
                  justify='left').grid(row=row, column=0, columnspan=3,
                                       sticky='w', padx=8, pady=(0, 16))
        row += 1

        ttk.Separator(body, orient='horizontal').grid(
            row=row, column=0, columnspan=3, sticky='ew', padx=8, pady=(4, 12))
        row += 1

        ttk.Label(body, text='恢复出厂设置', font=F_GROUP, foreground=INK).grid(
            row=row, column=0, columnspan=3, sticky='w', padx=8, pady=(0, 6))
        row += 1
        ttk.Label(body,
                  text='会删掉根目录的 config_local.json，所有设置回到程序自带的默认值'
                       '（会先备份一份）。',
                  font=F_HINT, foreground=WARN_COLOR, wraplength=740,
                  justify='left').grid(row=row, column=0, columnspan=3,
                                       sticky='w', padx=8, pady=(0, 6))
        row += 1
        ttk.Button(body, text='恢复默认…', command=self._on_reset).grid(
            row=row, column=0, sticky='w', padx=8, pady=(0, 16))

    def _build_records(self, body: ttk.Frame, snapshot: Dict[str, Any]) -> None:
        """
        「记录」页：上面是识别历史列表，下面是保留策略。

        对标 Typeless 的 History —— 但数据源是**我们已有的日记归档**
        （`年份/月份/日期.md` 与 `日期-角色.md`），不新建数据格式。
        """
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        try:
            from core.ui.record_page import RecordPage
            browser = RecordPage(body, insert_text=self._insert_text)
            browser.grid(row=0, column=0, sticky='nsew', padx=4, pady=(8, 0))
            self._record_browser = browser
        except Exception as e:
            logger.warning(f'历史列表加载失败: {e}')
            ttk.Label(body, text=f'历史列表不可用：{e}', font=F_HINT,
                      foreground=HINT_COLOR).grid(row=0, column=0, sticky='w',
                                                  padx=8, pady=8)

        holder = ttk.Frame(body)
        holder.grid(row=1, column=0, sticky='ew', padx=8, pady=(6, 8))
        row = 0
        for group in S.groups_in_tier('records'):
            row = self._build_group(holder, group, snapshot, row)

    def _on_tab_changed(self, event: tk.Event) -> None:
        """
        切页时刷新。

        「热词」页必须重读 hot.txt：「记录」页的「纠正」会把学到的词**直接写进文件**
        （不走本面板的保存流程），所以这一页的内容随时可能已经过期。
        「记录」页也重读一遍 —— 面板开着的时候可能又识别了几句。
        """
        try:
            nb = event.widget
            title = nb.tab(nb.select(), 'text')
        except Exception:
            return

        if title == '热词':
            try:
                from core.client.hotword.hot_file import read_hot
                text = read_hot()
                if text is not None and self._hot_manual is not None \
                        and self._hot_manual.get('1.0', 'end-1c') != text:
                    self._set_hot_text(text)
            except Exception as e:
                logger.debug(f'刷新热词页失败: {e}')
        elif title == '记录':
            try:
                page = getattr(self, '_record_browser', None)
                if page is not None:
                    page.reload()
            except Exception as e:
                logger.debug(f'刷新记录页失败: {e}')

    def _open_logs(self) -> None:
        self._open_dir(Path(REPO_ROOT) / 'logs')

    def _open_install_dir(self) -> None:
        self._open_dir(Path(REPO_ROOT))

    @staticmethod
    def _open_dir(path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))            # noqa: S606 (Windows only)
        except Exception as e:
            logger.debug(f'打开目录失败 {path}: {e}')

    @staticmethod
    def _gpu_summary() -> str:
        """
        [补丁] 把服务端探测到的显卡与推理后端摘要成一行。

        数据来自 logs/gpu_probe.json —— 服务端启动时由 core/tools/gpu_probe.py
        写入（探测 + 自动选择后端）。读不到（服务端还没跑过）就返回空串，
        界面不显示这一行。
        """
        import json
        path = Path(REPO_ROOT) / 'logs' / 'gpu_probe.json'
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return ''
        decision = data.get('decision') or {}
        if not decision or decision.get('error'):
            return ''

        names = [g.get('name', '') for g in (data.get('gpus') or [])
                 if not g.get('virtual') and g.get('name')]
        hardware = '、'.join(names[:3]) if names else '未识别到物理显卡'
        if len(names) > 3:
            hardware += f' 等 {len(names)} 块'

        if decision.get('disabled'):
            return f'硬件：{hardware}　（显卡自动选择已关闭，当前按配置里的固定值跑）'

        onnx = decision.get('onnx_provider') or '?'
        onnx_by = '自动' if decision.get('onnx_source') == '自动' else '指定'
        llm = '用显卡' if decision.get('llm_use_gpu') else '用 CPU'
        backend = decision.get('llama_backend') or '?'
        text = (f'硬件：{hardware}\n'
                f'推理后端：ONNX 编码器 {onnx}（{onnx_by}）　'
                f'GGUF 解码器 {llm}（llama.cpp {backend}）\n'
                f'想强制改用 CPU：在 config_local.json 的 server 段写 '
                f'{{"gpu_auto": false, "onnx_provider": "cpu"}}，重启服务端生效。')
        # 有后端"装了但用不起来"时，把原因也显示出来 —— 否则用户只会觉得"怎么这么慢"
        rejected = decision.get('rejected') or []
        if rejected:
            names_r = '、'.join(p.replace('ExecutionProvider', '') for p in rejected)
            text += (f'\n注意：{names_r} 被 onnxruntime 报告为可用，但实际会静默回落到 CPU'
                     f'（依赖没装全或版本不匹配），已跳过。')
        return text

    @staticmethod
    def _model_summary() -> tuple:
        """
        [补丁] 自检结果：(文案, 是否正常)。

        现场检查（只做文件存在性判断，很便宜），所以界面里显示的永远是最新状态 ——
        用户按了 CapsLock 没反应时，打开这里就能看到是不是缺文件。
        """
        try:
            from core.client.model_precheck import ENGINE_NAMES, format_lines, run_precheck
            report = run_precheck()
        except Exception:
            return '', True

        if report.ok:
            name = ENGINE_NAMES.get(report.model_type, report.model_type)
            return f'自检：模型与推理库齐备（{name}）', True

        lines = format_lines(report)
        return '自检发现问题：\n' + '\n'.join(lines[:6]), False

    # ---------------- 开机自启 ----------------

    def _refresh_autostart(self) -> None:
        """把按钮文字刷成当前状态。"""
        try:
            import autostart
            on = autostart.is_enabled()
        except Exception:
            on = False
        try:
            self._autostart_btn.configure(
                text='关闭开机自启' if on else '开启开机自启')
        except Exception:
            pass

    def _toggle_autostart(self) -> None:
        """
        开/关开机自启。

        两种形态的目标不同（见 autostart.link_target）：源码形态指向
        「启动 Speak More.vbs」（包一层 .lnk 是为了图标），打包形态直接指向
        Speak More.exe。
        """
        try:
            import autostart
            if autostart.is_enabled():
                autostart.disable()
                msg = '已关闭开机自启。'
            else:
                err = autostart.enable()
                msg = (f'开启失败：{err}' if err
                       else '已开启开机自启，下次开机会自动运行。')
        except Exception as e:
            msg = f'操作失败：{e}'
        logger.info(f'开机自启：{msg}')
        try:
            messagebox.showinfo('开机自启', msg, parent=self._dialog)
        except Exception:
            pass
        self._refresh_autostart()

    # ---------------- 「热词」页 ----------------

    def _build_hotwords(self, body: ttk.Frame, snap: Dict[str, Any]) -> None:
        """
        「热词」页：编辑 hot.txt。

        热词表的作用有两层，写清楚给用户看：
          ① **本地强制替换** —— 命中的词在识别结果里直接换掉，跑在大模型之前，
             瞬间、确定性、不花钱；
          ② **作为上下文喂给大模型** —— 命中的词会以「热词列表：[...]」的形式
             进提示词。这一层是给大模型补它拿不到的信息。

        ②比①重要得多，实测：
          - 大模型不知道的词（同事「老汪」其实是「老王」）：
            没有热词时 8 次全改不动；把「老王」放进热词表后 8/8 全改对。
          - 大模型偶尔改坏写对的专有名词（「松鼠云」被改成「松树云」，8 次里 2 次）：
            把「松鼠云」放进热词表后 8/8 全保住。

        所以这一页的定位是「告诉大模型你的词」，不是「替大模型干活」。

        ⚠️ 读不到 hot.txt 时**整个不建编辑框**。建了的话用户点保存 =
        用空内容覆盖掉磁盘上的文件。保持 _hot_manual 为 None，
        _collect_hotwords() 就会返回 None，调用方也就不会去动文件。
        """
        body.columnconfigure(0, weight=1)
        row = 0

        hw = snap.get('hotwords') or {}
        manual_text = hw.get('manual')

        ttk.Label(body, text='热词表 hot.txt', font=F_GROUP, foreground=INK).grid(
            row=row, column=0, sticky='w', padx=8, pady=(14, 2))
        row += 1
        ttk.Label(
            body,
            text=('把你自己的人名、公司名、项目代号、专业术语写进来。\n'
                  '大模型不认识这些词 —— 你不告诉它，它就没法改对，甚至可能改坏。\n'
                  '命中之后有两个作用：① 本地直接替换（快、每次都一样）；'
                  '② 作为「热词列表」喂给\n大模型，让它知道这个写法是对的。'),
            font=F_HINT, foreground=HINT_COLOR, wraplength=740,
            justify='left').grid(row=row, column=0, sticky='w', padx=8, pady=(0, 8))
        row += 1

        if manual_text is None:
            ttk.Label(body, text='（读不到 hot.txt，这一页暂时不可用）',
                      font=F_SMALL, foreground=WARN_COLOR).grid(
                row=row, column=0, sticky='w', padx=8, pady=(0, 12))
            return

        # ============================================================
        #  [补丁] 词典：词条列表（对标 Typeless 的 Dictionary）
        #
        #  原文编辑器仍然是**唯一的数据源** —— 这里的增删改都是先改
        #  `_hot_manual` 里的文本、再重画列表，保存时照旧只提交那份文本。
        #  所以不会出现"列表改了但没保存"或"两份数据打架"。
        # ============================================================
        ttk.Label(body, text='词典', font=F_GROUP, foreground=INK).grid(
            row=row, column=0, sticky='w', padx=8, pady=(4, 2))
        row += 1

        # [补丁] 「从我的纠正里学词」开关（对标 Typeless 的 Auto-added）。
        #
        # 这一项由这里手工画（它属于词典区），但字段本身在 settings_schema 里
        # 声明过，_collect() 照旧按 key 收得走 —— 所以保存流程不用动。
        #
        # 界面上必须把「读什么、什么时候读」说清楚：它有能力读到别的窗口里的
        # 文字，用户有权知道边界在哪。
        learn_var = self._new_var('bool', bool(
            (snap.get('client') or {}).get('learn_from_corrections', True)))
        cb = ttk.Checkbutton(body, text='从我的纠正里学词（打完字后你自己改过的地方）',
                             variable=learn_var)
        cb.grid(row=row, column=0, sticky='w', padx=8, pady=(0, 0))
        self._vars['learn_from_corrections'] = learn_var
        self._widgets['learn_from_corrections'] = cb
        row += 1
        ttk.Label(
            body,
            text=('你打完字之后如果又改了哪个词，我会「盯着那个输入框看一会儿」，'
                  '一有变化就记进词典 —— 下次说错也照样能改对。\n'
                  '改完立刻回车发送也没关系：输入框空了，我就用你发送前的最后样子结算。\n'
                  '边界：只看「刚打完字」之后那一小会儿（不是常驻监控）；'
                  '焦点跑到别的窗口就不看；只在你本机比对，不联网上传。\n'
                  '（系统里没有无障碍接口的控件读不到，读不到就什么都不做。）'),
            font=F_HINT, foreground=HINT_COLOR, wraplength=740,
            justify='left').grid(row=row, column=0, sticky='w', padx=8, pady=(0, 8))
        row += 1

        add_bar = ttk.Frame(body)
        add_bar.grid(row=row, column=0, sticky='ew', padx=8, pady=(0, 6))
        row += 1
        ttk.Label(add_bar, text='新词', font=F_SMALL,
                  foreground=HINT_COLOR).pack(side='left', padx=(0, 6))
        self._word_new_var = tk.StringVar()
        ent = ttk.Entry(add_bar, textvariable=self._word_new_var, width=24)
        ent.pack(side='left')
        ent.bind('<Return>', lambda _e: self._add_word_from_entry())
        ttk.Button(add_bar, text='添加', width=6,
                   command=self._add_word_from_entry).pack(side='left', padx=(6, 12))
        ttk.Button(add_bar, text='从 CSV 导入…',
                   command=self._import_words_csv).pack(side='left')
        self._word_count = ttk.Label(add_bar, text='', font=F_HINT,
                                     foreground=HINT_COLOR)
        self._word_count.pack(side='right')

        list_wrap = ttk.Frame(body)
        list_wrap.grid(row=row, column=0, sticky='ew', padx=8, pady=(0, 8))
        list_wrap.columnconfigure(0, weight=1)
        row += 1
        self._word_canvas = tk.Canvas(list_wrap, height=150, bg='white',
                                      highlightthickness=1,
                                      highlightbackground='#dddddd')
        self._word_canvas.grid(row=0, column=0, sticky='ew')
        sb = ttk.Scrollbar(list_wrap, orient='vertical',
                           command=self._word_canvas.yview)
        sb.grid(row=0, column=1, sticky='ns')
        self._word_canvas.configure(yscrollcommand=sb.set)
        self._word_list = tk.Frame(self._word_canvas, bg='white')
        self._word_list_id = self._word_canvas.create_window(
            (0, 0), window=self._word_list, anchor='nw')
        self._word_list.bind('<Configure>', lambda _e: self._word_canvas.configure(
            scrollregion=self._word_canvas.bbox('all')))
        self._word_canvas.bind('<Configure>', lambda e: self._word_canvas.itemconfigure(
            self._word_list_id, width=e.width))

        ttk.Label(body,
                  text='写法：每行一个词，用 | 分隔别名 —— `宇树科技 | 雨树科技 | 语数科技`；'
                       '要阻止某个替换，在该行后面加 `~~~ 要保护的词`；# 开头的是注释。',
                  font=F_HINT, foreground=HINT_COLOR, wraplength=740,
                  justify='left').grid(row=row, column=0, sticky='w', padx=8,
                                       pady=(0, 6))
        row += 1

        ttk.Label(body, text='高级：直接编辑原文', font=F_GROUP, foreground=INK).grid(
            row=row, column=0, sticky='w', padx=8, pady=(8, 2))
        row += 1
        self._hot_manual = tk.Text(body, height=12, wrap='none', font=F_MONO,
                                   relief='solid', borderwidth=1)
        self._hot_manual.grid(row=row, column=0, sticky='ew', padx=8, pady=(0, 6))
        self._hot_manual.insert('1.0', manual_text)
        self._hot_manual.bind('<KeyRelease>', lambda _e: self._refresh_word_list())
        row += 1

        # 可疑写法提示。热词表有个静默的坑：一个词完整包含另一个词
        # （`宇树科技 | 宇树`）会让用户每说一次"宇树"都被补成"宇树科技"。
        # 文件本身看不出问题，纠错也不报错，所以在这里体检一下。
        self._hot_lint = ttk.Label(body, text='', font=F_HINT,
                                   foreground=WARN_COLOR, wraplength=740,
                                   justify='left')
        self._hot_lint.grid(row=row, column=0, sticky='w', padx=8, pady=(0, 6))
        row += 1

        ttk.Button(body, text='打开所在文件夹',
                   command=lambda: self._open_dir(Path(REPO_ROOT))).grid(
            row=row, column=0, sticky='w', padx=8, pady=(0, 16))

        self._refresh_word_list()
        self._refresh_hot_lint()

    # ---------------- 词典：词条操作 ----------------

    def _hot_text(self) -> str:
        """当前原文编辑器里的内容（词典操作的唯一数据源）。"""
        try:
            return self._hot_manual.get('1.0', 'end-1c') if self._hot_manual else ''
        except Exception:
            return ''

    def _set_hot_text(self, text: str) -> None:
        """写回原文编辑器并刷新列表与体检。"""
        if self._hot_manual is None:
            return
        try:
            self._hot_manual.delete('1.0', 'end')
            self._hot_manual.insert('1.0', text)
        except Exception:
            return
        self._refresh_word_list()
        self._refresh_hot_lint()

    def _refresh_word_list(self) -> None:
        """从原文解析词条并重画列表。"""
        if getattr(self, '_word_list', None) is None:
            return
        for w in self._word_list.winfo_children():
            try:
                w.destroy()
            except Exception:
                pass
        try:
            from core.client.hotword.hot_file import parse_words
            words = parse_words(self._hot_text())
        except Exception as e:
            words = []
            logger.debug(f'解析词条失败: {e}')

        try:
            self._word_count.configure(text=f'共 {len(words)} 条')
        except Exception:
            pass

        if not words:
            tk.Label(self._word_list, text='（还没有词条）', font=F_HINT,
                     bg='white', fg=HINT_COLOR).pack(anchor='w', padx=6, pady=4)
            return

        for w in words:
            line = tk.Frame(self._word_list, bg='white')
            line.pack(fill='x', padx=6, pady=1)
            tk.Button(line, text='删除', font=F_HINT, bd=0, bg='white',
                      fg=WARN_COLOR, activebackground='white', cursor='hand2',
                      command=lambda ww=w: self._delete_word(ww)
                      ).pack(side='right', padx=(4, 0))
            detail = []
            if w.aliases:
                detail.append('别名：' + '、'.join(w.aliases))
            if w.blocked:
                detail.append('保护：' + '、'.join(w.blocked))
            body_text = f'　{"　".join(detail)}' if detail else ''
            tk.Label(line, text=f'{w.target}{body_text}', font=F_SMALL, bg='white',
                     fg=INK if not detail else HINT_COLOR, anchor='w',
                     justify='left').pack(side='left', fill='x', expand=True)

    def _add_word_from_entry(self) -> None:
        """把输入框里的词加进词典。"""
        target = (self._word_new_var.get() or '').strip()
        if not target:
            return
        try:
            from core.client.hotword.hot_file import add_word, find_word
            text = self._hot_text()
            if find_word(text, target):
                messagebox.showinfo('词典', f'「{target}」已经在词典里了。',
                                    parent=self._dialog)
                return
            # 支持在输入框里直接用 | 写别名：`正确写法 | 别名1 | 别名2`
            parts = [p.strip() for p in target.split('|') if p.strip()]
            text, ok = add_word(text, parts[0], parts[1:])
            if not ok:
                return
            self._set_hot_text(text)
            self._word_new_var.set('')
            logger.info(f'词典：新增词条 {parts[0]}')
            messagebox.showinfo('词典',
                                f'已添加「{parts[0]}」。\n\n'
                                '点「保存」后生效（保存即热生效，不用重启）。',
                                parent=self._dialog)
        except Exception as e:
            logger.warning(f'添加词条失败: {e}')
            messagebox.showwarning('词典', f'添加失败：{e}', parent=self._dialog)

    def _delete_word(self, word: Any) -> None:
        try:
            from core.client.hotword.hot_file import remove_word
            if not messagebox.askyesno('删除词条',
                                       f'从词典里删掉「{word.target}」？',
                                       parent=self._dialog):
                return
            self._set_hot_text(remove_word(self._hot_text(), word.line_no))
            logger.info(f'词典：删除词条 {word.target}')
        except Exception as e:
            logger.warning(f'删除词条失败: {e}')

    def _import_words_csv(self) -> None:
        """从 CSV 导入词条（每行第一列是正确写法，其余列是别名）。"""
        try:
            from tkinter import filedialog
            from core.client.hotword.hot_file import import_csv
            path = filedialog.askopenfilename(
                parent=self._dialog, title='选择词条 CSV',
                filetypes=[('CSV 文件', '*.csv'), ('文本文件', '*.txt'),
                           ('所有文件', '*.*')])
            if not path:
                return
            raw = Path(path).read_text(encoding='utf-8-sig', errors='replace')
            text, added, skipped = import_csv(self._hot_text(), raw)
            if added:
                self._set_hot_text(text)
            logger.info(f'词典：CSV 导入新增 {added} 条，跳过 {len(skipped)} 条')
            msg = f'导入完成：新增 {added} 条。'
            if skipped:
                msg += f'\n\n已在词典里、跳过的 {len(skipped)} 条：\n' + '、'.join(skipped[:8])
                if len(skipped) > 8:
                    msg += f' …等 {len(skipped)} 条'
            msg += '\n\n点「保存」后生效。'
            messagebox.showinfo('词典', msg, parent=self._dialog)
        except Exception as e:
            logger.warning(f'导入 CSV 失败: {e}')
            messagebox.showwarning('词典', f'导入失败：{e}', parent=self._dialog)

    def _refresh_hot_lint(self) -> None:
        """跑一遍热词表体检，把可疑行显示出来。"""
        if self._hot_manual is None or self._hot_lint is None:
            return
        try:
            from core.client.hotword.hot_file import lint
            warns = lint(self._hot_manual.get('1.0', 'end-1c'))
        except Exception:
            warns = []
        try:
            self._hot_lint.configure(text=('\n'.join(warns) if warns else ''))
        except Exception:
            pass

    def _collect_hotwords(self) -> Optional[Dict[str, Any]]:
        """
        收集这一页的内容。返回 None 表示"这一页没建出来，别动磁盘上的文件"。

        ⚠️ 返回 None 这件事很重要：`_collect()` 的结果会被当成"用户想要的
        全部配置"落盘。要是这页没建（比如测试里只建了别的页），返回个空串
        就等于**把用户的热词表清空了**。
        """
        if self._hot_manual is None:
            return None
        try:
            return {'manual': self._hot_manual.get('1.0', 'end-1c')}
        except Exception:
            return None


    def _build_llm_test(self, body: ttk.Frame, row: int) -> int:
        """
        连通性测试。

        用的是**界面上当前填的值**，而不是已保存的值 —— 用户填完
        地址和 Key 应该能立刻试通再保存，而不是先存再回来试。
        """
        holder = ttk.Frame(body)
        holder.grid(row=row, column=0, columnspan=3, sticky='ew',
                    padx=8, pady=(4, 8))

        self._btn_llm_test = ttk.Button(holder, text='测试连接', width=12,
                                        command=self._on_test_llm)
        self._btn_llm_test.pack(side='left')

        self._llm_test_label = tk.Label(holder, text='', font=F_SMALL,
                                        bg='white', fg=HINT_COLOR,
                                        wraplength=520, justify='left')
        self._llm_test_label.pack(side='left', padx=10)
        return 1

    def _on_test_llm(self) -> None:
        url = str(self._vars.get('llm.api_url').get()).strip() if self._vars.get('llm.api_url') else ''
        key = str(self._vars.get('llm.api_key').get()).strip() if self._vars.get('llm.api_key') else ''
        model = str(self._vars.get('llm.model').get()).strip() if self._vars.get('llm.model') else ''

        self._btn_llm_test.configure(state='disabled')
        self._llm_test_label.configure(text='正在测试…', fg=HINT_COLOR)

        # 工作线程只写这个槽，结果由 Tk 线程轮询取 —— 工作线程直接 after()
        # 在对话框打开时（主线程在 wait_window）会卡住/抛异常，然后被吞掉，
        # 表现就是"点了测试连接没反应"。见 core/ui/dialogs.py:ThreadSlot
        from core.ui.dialogs import ThreadSlot, poll_slot
        slot = ThreadSlot()

        def worker() -> None:
            try:
                # 延迟导入，避免 UI 层在 import 期就依赖客户端的 LLM 子系统
                from core.client.llm.llm_test import test_connection
                from core.ui import settings_schema as _S
                ok, msg = test_connection(_S.GENERIC_PROVIDER, url, key, model)
            except Exception as e:
                ok, msg = False, f'测试出错: {e}'
            slot.put((bool(ok), str(msg)))

        threading.Thread(target=worker, daemon=True, name='LLMTestPanel').start()
        poll_slot(self._dialog, slot, self._on_llm_test_done, timeout_s=60.0)

    def _on_llm_test_done(self, result: Any) -> None:
        """测试连接的结果回到 Tk 线程之后（result 为 None 表示超时）。"""
        ok, msg = result if result else (False, '测试超时，没有收到结果')
        try:
            self._llm_test_label.configure(text=msg,
                                           fg='#15803d' if ok else '#b91c1c')
            self._btn_llm_test.configure(state='normal')
        except Exception:
            pass

    def _build_mic_test(self, body: ttk.Frame, row: int) -> int:
        """
        麦克风那一栏的「测试麦克风」。

        用的是界面上当前选中的设备（会先把它应用上去），不是已保存的值 ——
        用户换个设备应该能立刻试，而不用"保存 → 重启音频流 → 再回来听"。
        """
        holder = ttk.Frame(body)
        holder.grid(row=row, column=0, columnspan=3, sticky='ew',
                    padx=8, pady=(4, 2))

        self._mic_test = MicTestPanel(
            holder,
            get_device=self._current_mic_device,
            mic_apply=self._mic_apply,
            bg='white', font=F_SMALL)
        self._mic_test.pack(fill='x')

        ttk.Label(body,
                  text='换个设备先点这里试一下，免得保存完才发现选错了。',
                  font=F_HINT, foreground=HINT_COLOR).grid(
            row=row + 1, column=0, columnspan=3, sticky='w',
            padx=8, pady=(0, 8))
        return 2

    def _current_mic_device(self) -> str:
        """界面上当前选中的设备名（显示名 -> 真实值）。"""
        var = self._vars.get('mic_device')
        if var is None:
            return ''
        mapping = self._choice_maps.get('mic_device') or {}
        return str(mapping.get(str(var.get()), '') or '')

    def _value_of(self, f: S.Field, snap: Dict[str, Any]) -> Any:
        """
        取字段当前值。

        角色字段（llm.*）做两处修正：
          - 作者在 LLM/*.py 里留的脱敏假 key（一串 *）当空，否则用户
            会以为已经配好了；
          - 空值补默认值。Base URL 现在是唯一的连接信息，空着一定是错的，
            给个能用的起点比让用户对着空框发呆好。
        """
        if not S.is_role_field(f.key):
            return snap['client'].get(f.key, f.default)

        current = self._current_role_values().get(S.role_attr(f.key), f.default)
        if S.is_placeholder_secret(current):
            current = ''
        return f.default if current in (None, '') else current

    def _build_field(self, body: ttk.Frame, f: S.Field,
                     snap: Dict[str, Any], row: int) -> int:
        current = self._value_of(f, snap)

        if f.key == 'shortcuts':
            ttk.Label(body, text=f.label, font=F_NORMAL).grid(
                row=row, column=0, sticky='nw', padx=(8, 10), pady=6)
            self._build_shortcut_editor(body, current, row)
            next_row = row + 4

        elif f.kind == 'bool':
            # 勾选框自带标签，而且标签本身可以点 ——
            # 原来是"标签在左、一个光秃秃的方框在右"，那个方框太容易被
            # 当成空白格子忽略掉（用户反馈过"没看到可以勾选的框"）。
            var = self._new_var('bool', bool(current))
            cb = ttk.Checkbutton(body, text=f.label, variable=var)
            cb.grid(row=row, column=0, columnspan=2, sticky='w',
                    padx=8, pady=6)
            self._vars[f.key] = var
            self._widgets[f.key] = cb
            next_row = row + 1

        elif f.stacked:
            # URL / Key 这类长字段：标签单独一行，输入框占满下一行
            ttk.Label(body, text=f.label, font=F_NORMAL).grid(
                row=row, column=0, columnspan=2, sticky='w',
                padx=8, pady=(8, 0))
            self._build_simple(body, f, current, row + 1, span=True)
            next_row = row + 2

        else:
            ttk.Label(body, text=f.label, font=F_NORMAL).grid(
                row=row, column=0, sticky='nw', padx=(8, 10), pady=6)
            self._build_simple(body, f, current, row, span=False)
            next_row = row + 1

        hint_parts = [f.hint] if f.hint else []
        if f.effect != 'hot':
            hint_parts.append(S.EFFECT_TEXT.get(f.effect, ''))
        if hint_parts:
            ttk.Label(body, text='  '.join(h for h in hint_parts if h),
                      font=F_HINT,
                      foreground=WARN_COLOR if f.effect != 'hot' else HINT_COLOR,
                      wraplength=700, justify='left').grid(
                row=next_row, column=0, columnspan=2,
                sticky='w', padx=8, pady=(0, 8))
            next_row += 1

        return next_row - row

    def _current_role_values(self) -> Dict[str, Any]:
        return self._role_cache.get(self._role_var.get() if self._role_var
                                    else self._current_role, {})

    # ---------------- Tk 变量 ----------------

    def _new_var(self, kind: str, value: Any = None) -> tk.Variable:
        """
        建 Tk 变量 —— **一律显式绑定 master**，不要图省事用 `tk.BooleanVar()`。

        不传 master 时 Tkinter 会用模块级的 `_default_root`。这个程序里有
        好几个 Tk 根（toast 管理器、录音浮层、上下文对话框、本面板各一个），
        `_default_root` 指向哪个完全看谁最后建 —— 而根是分线程建的。

        变量一旦挂到**别的解释器**上，勾选框在自己的解释器里读不到它，
        就会被当成"未设置"，渲染成三态的横线：
        点一下变成空白、再点一下才打勾。用户看到的就是"这个勾选框坏了"。
        """
        cls = tk.BooleanVar if kind == 'bool' else tk.StringVar
        if value is None:
            return cls(master=self._dialog)
        return cls(master=self._dialog, value=value)

    # ---------------- 各类控件 ----------------

    def _build_simple(self, body: ttk.Frame, f: S.Field, current: Any,
                      row: int, span: bool = False) -> None:
        """
        普通输入控件。

        span=True 表示"占满整行"（标签已经在上一行）：先套一个 Frame
        横跨所有列，里面的控件再自己排，这样"输入框 + 右边小按钮"
        这种组合也摆得开。
        """
        key = f.key

        if span:
            holder = ttk.Frame(body)
            holder.grid(row=row, column=0, columnspan=3, sticky='ew',
                        padx=8, pady=(2, 0))
            holder.columnconfigure(0, weight=1)
            parent, grow = holder, 0
        else:
            parent, grow = body, row

        first_col = 0 if span else 1
        pad_right = 0 if span else 8

        if f.kind == 'multiline':
            w = tk.Text(parent, height=4, wrap='word', font=F_NORMAL,
                        relief='solid', borderwidth=1)
            w.insert('1.0', '' if current is None else str(current))
            w.grid(row=grow, column=first_col,
                   columnspan=2 if not span else 1,
                   sticky='ew', pady=6, padx=(0, pad_right))
            self._multiline[key] = w
            self._widgets[key] = w
            return

        if f.kind == 'secret':
            var = self._new_var('str', '' if current is None else str(current))
            w = ttk.Entry(parent, textvariable=var, show='*', width=46, font=F_MONO)
            w.grid(row=grow, column=first_col, sticky='ew',
                   pady=6, padx=(0, 4 if span else 8))
            ttk.Button(parent, text='👁', width=3,
                       command=lambda e=w: e.configure(
                           show='' if e.cget('show') else '*')).grid(
                row=grow, column=first_col + 1, sticky='w')
            self._vars[key] = var
            self._widgets[key] = w
            return

        if f.kind == 'choice':
            self._build_choice(parent, f, current, grow, span=span)
            return

        if f.kind in ('float', 'int'):
            var = self._new_var('str', str(current if current is not None
                                           else f.default))
            w = ttk.Entry(parent, textvariable=var, width=14)
            w.grid(row=grow, column=first_col, sticky='w', pady=6)
            if f.kind == 'float':
                ttk.Label(parent, text='秒', font=F_HINT,
                          foreground=HINT_COLOR).grid(
                    row=grow, column=first_col + 1, sticky='w', padx=4)
            self._vars[key] = var
            self._widgets[key] = w
            return

        # str
        var = self._new_var('str', '' if current is None else str(current))
        w = ttk.Entry(parent, textvariable=var, width=46)
        w.grid(row=grow, column=first_col,
               columnspan=2 if not span else 1,
               sticky='ew', pady=6, padx=(0, pad_right))
        self._vars[key] = var
        self._widgets[key] = w

    def _build_choice(self, body: ttk.Frame, f: S.Field, current: Any,
                      row: int, span: bool = False) -> None:
        # 全部是只读下拉。之前 llm.model 在这里开了可编辑模式（模型名允许
        # 自己敲），后来用户要求模型彻底不要下拉框，它就改成 kind='str'
        # 走普通文本框了（见 settings_schema.py），这条分支随之取消。
        if f.key == 'mic_device':
            choices = self._device_choices or [('系统默认', '')]
        else:
            choices = list(f.choices)

        labels = [c[0] for c in choices]
        mapping = {c[0]: c[1] for c in choices}
        self._choice_maps[f.key] = mapping

        var = self._new_var('str')
        combo = ttk.Combobox(body, textvariable=var, values=labels, width=38,
                             state='readonly')
        shown = next((lbl for lbl, val in mapping.items() if val == current), None)
        if shown is None:
            shown = str(current) if current not in (None, '') else labels[0]
        var.set(shown)
        combo.grid(row=row, column=0 if span else 1, sticky='w', pady=6)

        self._vars[f.key] = var
        self._widgets[f.key] = combo

        if f.key == 'mic_device':
            ttk.Button(body, text='刷新设备', command=self._reload_devices).grid(
                row=row, column=1 if span else 2, sticky='w', padx=4)

    # ---------------- 快捷键编辑器 ----------------

    def _build_shortcut_editor(self, body: ttk.Frame, current: Any, row: int) -> None:
        container = ttk.Frame(body)
        container.grid(row=row, column=1, columnspan=2, sticky='ew',
                       pady=6, padx=(0, 8))

        # 只留三列：按键 / 怎么触发 / 启用。
        #
        # 之前还暴露了「阻塞」和「类型」，两个都是实现细节：
        #   - 阻塞(suppress)：按键要不要被程序吞掉。正常就该吞掉，
        #     吞掉之后短按还会自动补发，所以 CapsLock 照样能切换大小写。
        #     暴露给用户只会让人困惑，这里一律保持原值不动。
        #   - 类型(keyboard/mouse)：看按键名就知道了，x1/x2 是鼠标，
        #     其余是键盘。由 _key_to_type() 自动推导。
        header = ttk.Frame(container)
        header.pack(fill='x')
        for txt, w in (('按一下这个键', 26), ('怎么触发', 26), ('启用', 6)):
            ttk.Label(header, text=txt, font=F_HINT, foreground=HINT_COLOR,
                      width=w).pack(side='left')

        self._shortcut_holder = ttk.Frame(container)
        self._shortcut_holder.pack(fill='x')

        items = current if isinstance(current, list) else []
        for sc in items:
            if isinstance(sc, dict):
                self._add_shortcut_row(sc)

        buttons = ttk.Frame(container)
        buttons.pack(fill='x', pady=(6, 0))
        ttk.Button(buttons, text='＋ 添加', width=10,
                   command=lambda: self._add_shortcut_row(
                       {'key': 'f12', 'type': 'keyboard',
                        'suppress': True, 'hold_mode': True, 'enabled': True})
                   ).pack(side='left')
        ttk.Button(buttons, text='－ 删除末行', width=12,
                   command=self._remove_last_shortcut_row).pack(side='left', padx=6)

        # 捕获结果 / 失败原因显示在这里。
        # 不弹 messagebox：用户按错键是常事，弹窗要点一下才消失，很烦。
        self._shortcut_status = ttk.Label(container, text='', font=F_HINT,
                                          foreground=HINT_COLOR, wraplength=640,
                                          justify='left')
        self._shortcut_status.pack(fill='x', pady=(4, 0))

    def _add_shortcut_row(self, sc: Dict[str, Any]) -> None:
        holder = self._shortcut_holder
        if holder is None:
            return
        r = len(self._shortcut_rows)

        key_var = self._new_var('str', str(sc.get('key', 'f12')))
        mode_var = self._new_var(
            'str', MODE_HOLD if sc.get('hold_mode', True) else MODE_TOGGLE)
        en_var = self._new_var('bool', bool(sc.get('enabled', True)))

        row_frame = ttk.Frame(holder)
        row_frame.pack(fill='x')

        # 主路径：点一下，然后按下想用的键。
        # 用 pynput 而不是 Tk 的 <KeyPress> —— 鼠标侧键和会被应用自己吞掉的
        # CapsLock，Tk 那套都收不到。见 core/ui/key_capture.py。
        key_btn = ttk.Button(row_frame, width=20,
                             text=kc.key_label(key_var.get()))
        key_btn.pack(side='left')

        # 备用：极少数捕获不到的键（主要是 Esc —— 它被约定用来取消捕获）
        menu_btn = ttk.Button(row_frame, width=2, text='▾')
        menu_btn.pack(side='left', padx=(2, 10))

        ttk.Combobox(row_frame, textvariable=mode_var, values=list(_SHORTCUT_MODES),
                     width=24, state='readonly').pack(side='left', padx=(0, 10))

        ttk.Checkbutton(row_frame, variable=en_var).pack(side='left')

        row = {
            'key': key_var,
            'mode': mode_var,
            # 保留原值，界面上不给改
            'suppress': bool(sc.get('suppress', True)),
            'rows': r,
            'enabled': en_var,
            'btn': key_btn,
        }
        self._shortcut_rows.append(row)

        key_btn.configure(command=lambda _r=row: self._start_capture(_r))
        menu_btn.configure(command=lambda _r=row, _b=menu_btn:
                           self._show_key_menu(_r, _b))

    def _show_key_menu(self, row: Dict[str, Any], anchor: tk.Misc) -> None:
        """备用路径：从菜单里挑一个键（捕获不到 Esc 时用）。"""
        menu = tk.Menu(self._dialog, tearoff=0)
        groups = (
            ('修饰键', ('caps_lock', 'num_lock', 'scroll_lock',
                        'ctrl_l', 'ctrl_r', 'shift', 'shift_r',
                        'alt_l', 'alt_gr', 'cmd', 'cmd_r')),
            ('常见', ('space', 'enter', 'tab', 'backspace', 'delete', 'insert',
                      'esc', 'home', 'end', 'page_up', 'page_down',
                      'up', 'down', 'left', 'right',
                      'print_screen', 'pause', 'menu')),
            ('鼠标', ('x1', 'x2')),
        )
        for title, keys in groups:
            sub = tk.Menu(menu, tearoff=0)
            for k in keys:
                sub.add_command(label=kc.key_label(k),
                                command=lambda _k=k, _r=row: self._set_key(_r, _k))
            menu.add_cascade(label=title, menu=sub)

        sub_f = tk.Menu(menu, tearoff=0)
        for k in (f'f{i}' for i in range(1, 25)):
            sub_f.add_command(label=kc.key_label(k),
                              command=lambda _k=k, _r=row: self._set_key(_r, _k))
        menu.add_cascade(label='功能键 F1–F24', menu=sub_f)

        sub_a = tk.Menu(menu, tearoff=0)
        for k in 'abcdefghijklmnopqrstuvwxyz':
            sub_a.add_command(label=k.upper(),
                              command=lambda _k=k, _r=row: self._set_key(_r, _k))
        menu.add_cascade(label='字母', menu=sub_a)

        try:
            menu.tk_popup(anchor.winfo_rootx(),
                          anchor.winfo_rooty() + anchor.winfo_height())
        finally:
            menu.grab_release()

    def _set_key(self, row: Dict[str, Any], key: str) -> None:
        row['key'].set(key)
        btn = row.get('btn')
        if btn is not None:
            try:
                btn.configure(text=kc.key_label(key))
            except Exception:
                pass

    # ---------------- 按一下键就捕获 ----------------

    def _start_capture(self, row: Dict[str, Any]) -> None:
        """
        进入捕获态：用户按哪个键，就把这一行的按键设成哪个。

        在**后台线程**里跑捕获（`capture_next_key` 是阻塞的），Tk 线程靠
        `after` 轮询结果 —— 阻塞 Tk 线程的话界面会整个冻住（按钮上的
        "请按下一个键…"都画不出来）。
        """
        if self._capturing_row is not None:
            return                                   # 已经在捕获了，忽略重复点击
        if self._key_capture is None:
            self._set_status('按键捕获在这个环境里不可用，请用右边的 ▾ 手动选一个键。',
                             warn=True)
            return

        self._capturing_row = row
        self._capture_cancel = threading.Event()
        self._capture_queue = queue.Queue()

        btn = row.get('btn')
        if btn is not None:
            try:
                btn.configure(text='请按下一个键…')
            except Exception:
                pass
        self._set_status('现在按下你想用的那个键（鼠标侧键也可以）。'
                         '按 Esc 取消，15 秒没按会自动放弃。')

        # 捕获期间键盘焦点别留在输入框上 —— 否则按字母键会往框里打字
        try:
            self._dialog.focus_set()
        except Exception:
            pass

        app_capture = self._key_capture
        cancel = self._capture_cancel
        box = self._capture_queue

        def _work():
            try:
                res = app_capture(timeout=15.0, cancel=cancel)
            except Exception as e:                    # 兜底，绝不让线程静默死掉
                res = kc.CaptureResult(reason=f'按键捕获出错: {e}')
            try:
                box.put(res)
            except Exception:
                pass

        threading.Thread(target=_work, daemon=True).start()
        self._dialog.after(80, self._poll_capture)

    def _poll_capture(self) -> None:
        """Tk 线程这边轮询捕获结果。"""
        row = self._capturing_row
        if row is None:
            return
        try:
            res = self._capture_queue.get_nowait()
        except queue.Empty:
            self._dialog.after(80, self._poll_capture)
            return
        self._finish_capture(row, res)

    def _finish_capture(self, row: Dict[str, Any], res: Any) -> None:
        self._capturing_row = None
        cancelled_ev = self._capture_cancel
        self._capture_cancel = None

        if getattr(res, 'ok', False):
            self._set_key(row, res.key)
            self._set_status(f'已设为「{kc.key_label(res.key)}」。')
            return

        # 失败/取消/超时：按钮显示回原来的键
        try:
            row['btn'].configure(text=kc.key_label(row['key'].get()))
        except Exception:
            pass
        reason = getattr(res, 'reason', '') or '没捕获到按键，再试一次。'
        if cancelled_ev is not None and cancelled_ev.is_set():
            self._set_status('已取消。')
        else:
            self._set_status(reason, warn=True)

    def _cancel_capture(self) -> None:
        """把正在进行的捕获叫停（Esc 或面板关闭时）。"""
        if self._capture_cancel is not None:
            try:
                self._capture_cancel.set()
            except Exception:
                pass

    def _on_escape(self, _event: Any = None) -> str:
        """
        Esc 的两义性：**正在捕获时它取消捕获**，否则还是关面板。

        不这么做的话，用户想取消捕获反而把整个面板关了，
        改了一半的设置全丢 —— 这是最招人烦的失败模式。
        """
        if self._capturing_row is not None:
            self._cancel_capture()
            row = self._capturing_row
            try:
                row['btn'].configure(text=kc.key_label(row['key'].get()))
            except Exception:
                pass
            self._capturing_row = None
            self._set_status('已取消。')
            return 'break'                 # 别让 Esc 冒泡去关面板
        self._on_cancel()
        return 'break'

    def _set_status(self, text: str, warn: bool = False) -> None:
        if self._shortcut_status is None:
            return
        try:
            self._shortcut_status.configure(
                text=text, foreground=WARN_COLOR if warn else HINT_COLOR)
        except Exception:
            pass

    def _remove_last_shortcut_row(self) -> None:
        if not self._shortcut_rows or self._shortcut_holder is None:
            return
        gone = self._shortcut_rows.pop()
        if gone is self._capturing_row:
            self._cancel_capture()
            self._capturing_row = None
        children = self._shortcut_holder.winfo_children()
        if children:
            children[-1].destroy()

    # ---------------- 麦克风 ----------------

    def _reload_devices(self) -> None:
        try:
            from core.client.audio.device_select import list_input_devices
            devices = [('系统默认', '')] + [
                (f'{name}', name) for _idx, name, _ch in list_input_devices()
            ]
        except Exception:
            messagebox.showwarning('刷新设备失败',
                                   '无法枚举麦克风设备，请查看日志。',
                                   parent=self._dialog)
            return

        self._device_choices = devices
        mapping = {label: value for label, value in devices}
        self._choice_maps['mic_device'] = mapping

        combo = self._widgets.get('mic_device')
        if isinstance(combo, ttk.Combobox):
            current = mapping.get(self._vars['mic_device'].get(), '')
            combo.configure(values=[label for label, _ in devices])
            self._vars['mic_device'].set(
                next((lbl for lbl, val in mapping.items() if val == current),
                     devices[0][0]))

    # ---------------- 角色切换 ----------------

    def _switch_role(self) -> None:
        """把当前控件里的角色字段值存进缓存，再载入新角色的值。"""
        if self._role_var is None:
            return
        new_role = self._role_var.get()

        # 1) 先把旧角色的当前值收进缓存
        if self._current_role in self._role_cache:
            for attr in self._role_cache[self._current_role]:
                self._role_cache[self._current_role][attr] = self._read_role_widget(attr)

        # 2) 载入新角色
        self._current_role = new_role
        self._role_cache.setdefault(new_role, {})
        for attr in self._role_cache[new_role]:
            self._write_role_widget(attr, self._role_cache[new_role][attr])

    def _read_role_widget(self, attr: str) -> Any:
        key = f'llm.{attr}'
        var = self._vars.get(key)
        field = S.find_field(key)
        if var is None or field is None:      # 表里没有的字段原样保留
            return self._role_cache.get(self._current_role, {}).get(attr)
        raw = var.get()
        if field.kind == 'bool':
            return bool(raw)
        if field.kind == 'choice':
            mapping = self._choice_maps.get(key) or {}
            return mapping.get(str(raw), raw)
        return raw

    def _write_role_widget(self, attr: str, value: Any) -> None:
        key = f'llm.{attr}'
        var = self._vars.get(key)
        field = S.find_field(key)
        if var is None or field is None:
            return
        if field.kind == 'bool':
            var.set(bool(value))
        elif field.kind == 'choice':
            mapping = self._choice_maps.get(key) or {}
            var.set(next((lbl for lbl, val in mapping.items() if val == value),
                         str(value)))
        else:
            var.set('' if value is None else str(value))

    # ---------------- 收集 ----------------

    def _collect(self) -> Dict[str, Any]:
        client: Dict[str, Any] = {}
        llm_roles: Dict[str, Dict[str, Any]] = {}

        for f in S.all_fields():
            if f.key == 'shortcuts':
                client['shortcuts'] = self._collect_shortcuts()
                continue

            if S.is_role_field(f.key):
                continue        # 角色字段单独收集，见下

            if f.key in self._multiline:
                client[f.key] = self._multiline[f.key].get('1.0', 'end-1c')
                continue

            var = self._vars.get(f.key)
            if var is None:
                continue
            raw = var.get()
            if f.kind == 'choice':          # 下拉框里存的是显示名，要映射回真实值
                mapping = self._choice_maps.get(f.key) or {}
                raw = mapping.get(str(raw), raw)
            client[f.key] = self._coerce(f, raw)

        # 角色字段：先存当前编辑角色的最新值，再把全部角色的缓存吐出去
        current = self._role_var.get() if self._role_var else self._current_role
        for attr in self._role_cache.get(current, {}):
            self._role_cache[current][attr] = self._read_role_widget(attr)
        for name, values in self._role_cache.items():
            if values:
                llm_roles[name] = dict(values)

        out: Dict[str, Any] = {'client': client, 'llm_roles': llm_roles}

        # 「热词」页。返回 None 表示这一页没建出来 —— 那就**不能**往结果里
        # 放热词，否则调用方会把"空内容"当成用户的意图，把 hot.txt 清掉。
        hotwords = self._collect_hotwords()
        if hotwords is not None:
            out['hotwords'] = hotwords

        return out

    @staticmethod
    def _coerce(f: S.Field, raw: Any) -> Any:
        """把界面上的字符串转回正确类型。转不了就退回默认值。"""
        if f.kind == 'bool':
            return bool(raw)
        if f.kind in ('int', 'float'):
            try:
                return int(float(str(raw).strip())) if f.kind == 'int' \
                    else float(str(raw).strip())
            except (TypeError, ValueError):
                return f.default
        if f.kind == 'choice':
            return raw          # 已在 _collect 里通过 mapping 转过
        return str(raw)

    def _collect_shortcuts(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in self._shortcut_rows:
            key = str(row['key'].get()).strip()
            if not key:
                continue
            out.append({
                'key': key,
                # 类型由按键名推导；suppress 保留原值（界面上不暴露）
                'type': _key_to_type(key),
                'suppress': bool(row['suppress']),
                'hold_mode': str(row['mode'].get()) == MODE_HOLD,
                'enabled': bool(row['enabled'].get()),
            })
        return out

    # ---------------- 按钮 ----------------

    def _on_confirm(self) -> None:
        self.result = DialogResult(confirmed=True, **self._collect())
        self._close()

    def _on_cancel(self) -> None:
        self.result = DialogResult(confirmed=False)
        self._close()

    def _on_reset(self) -> None:
        # 留个痕迹：这个按钮一旦确认就会把整个 config_local.json 改名备份掉，
        # 用户改半天的东西全没了。排查"设置怎么不生效"时，这行日志是关键。
        try:
            from . import logger as _log
            _log.info('用户点击了「恢复默认」按钮')
        except Exception:
            pass

        if messagebox.askyesno(
                '恢复默认',
                '会删掉 config_local.json，所有设置回到程序自带的默认值。\n'
                '需要重启客户端才生效。\n\n确定吗？',
                parent=self._dialog):
            self.result = DialogResult(confirmed=True, reset=True)
            self._close()

    def _close(self) -> None:
        # 先把可能在跑的捕获叫停 —— 它跑在后台线程里，还会占着宿主的
        # 快捷键监听。不叫停的话，用户关掉面板之后快捷键要等最多 15 秒
        # （捕获超时）才会恢复，期间按什么键都没反应。
        self._cancel_capture()
        self._capturing_row = None

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

        为什么必须做：这些变量绑在**别的线程**的 Tk 解释器上
        （见 core/ui/settings_handler.py 的 SettingsDialogThread）。
        要是不管它，等到解释器退出时主线程的 GC 会去跑
        `tkinter.Variable.__del__`，而那个解释器正在 mainloop 里持着锁 ——
        主线程就**永久卡死**。表现是：面板用着完全正常，但程序退不出去。

        实测栈（py-spy）：
            Thread XXX (idle)
                __del__ (tkinter\\__init__.py:416)

        把 `_tk` 置空之后 `__del__` 会直接 return，不再碰 Tcl。
        此时窗口已经销毁，这些变量也不会再被用到。
        """
        for var in list(self._vars.values()):
            self._detach_var(var)
        for row in self._shortcut_rows:
            for key in ('key', 'mode', 'enabled'):
                self._detach_var(row.get(key))

        self._hot_manual = None
        self._hot_lint = None

        self._vars.clear()
        self._shortcut_rows.clear()
        self._widgets.clear()
        self._multiline.clear()
        self._choice_maps.clear()

    @staticmethod
    def _detach_var(var: Any) -> None:
        try:
            if var is not None and getattr(var, '_tk', None) is not None:
                var._tk = None
        except Exception:
            pass

    # ---------------- 杂项 ----------------

    @staticmethod
    def _center(win: tk.Toplevel, width: int, height: int) -> None:
        try:
            win.update_idletasks()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            width, height = min(width, sw - 40), min(height, sh - 80)
            x, y = max(0, (sw - width) // 2), max(0, (sh - height) // 2 - 30)
            win.geometry(f'{width}x{height}+{x}+{y}')
            win.deiconify()
            win.lift()
            win.focus_force()
        except Exception:
            try:
                win.deiconify()
            except Exception:
                pass


def show_settings_dialog(snapshot: Dict[str, Any],
                         parent: Optional[tk.Misc] = None) -> DialogResult:
    """便捷入口，和 core/ui/context_dialog.py 的写法保持一致。"""
    return SettingsDialog(parent).show(snapshot)
