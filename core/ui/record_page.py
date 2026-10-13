# coding: utf-8
"""
「记录」页：浏览识别历史与 LLM 对话记录。

数据来自已有的日记归档（解析见 `core.client.history`），**不新建数据格式**。

对标 Typeless 的 History：
  - 三类筛选：全部 / 听写 / 对话（对应日记的两种文件）
  - 每条可：复制 / 插入到当前光标 / 播放原始音频 / 删除
  - 搜索
保留策略（"只留最近 7 天"之类）是声明式字段，由设置面板统一渲染、统一保存。

本模块是纯 UI：`insert_text` 由 settings_handler 注入（要调 app 的异步输出），
拿不到时「插入」按钮自动禁用。
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, List, Optional

from . import logger

try:
    from core.client import history as H
except Exception:                                    # pragma: no cover
    H = None                                         # type: ignore

F_TEXT = ('Microsoft YaHei UI', 9)
F_TIME = ('Microsoft YaHei UI', 8)
F_HINT = ('Microsoft YaHei UI', 8)
HINT = '#6b7280'
INK = '#111827'
ACCENT = '#1d6fd0'
BAD = '#b91c1c'

PAGE_SIZE = 60            # 一屏先给这么多条，底下有「显示更多」


class RecordPage(ttk.Frame):
    """识别历史浏览器。"""

    def __init__(self, master: tk.Misc,
                 insert_text: Optional[Callable[[str], None]] = None,
                 bg: str = 'white', **kw: Any):
        super().__init__(master, **kw)
        self._insert = insert_text
        self._kind = ''
        self._keyword = ''
        self._limit = PAGE_SIZE
        self._entries: List[Any] = []
        self._rows: List[tk.Widget] = []

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._build()
        self.reload()            # 建完就先把历史读出来（漏了这行的话页面是空的）

    # ---------------- 界面搭建 ----------------

    def _build(self) -> None:
        # ---- 顶部：筛选 + 搜索 ----
        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky='ew', padx=8, pady=(0, 6))

        self._filter_btns = {}
        for key, label in (('', '全部'), (H.KIND_DICTATION if H else 'd', '听写'),
                           (H.KIND_CHAT if H else 'c', '对话')):
            btn = ttk.Button(top, text=label, width=6,
                             command=lambda k=key: self.set_kind(k))
            btn.pack(side='left', padx=(0, 4))
            self._filter_btns[key] = btn

        ttk.Label(top, text='搜索', font=F_HINT, foreground=HINT).pack(
            side='left', padx=(12, 4))
        self._search_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self._search_var, width=22)
        ent.pack(side='left')
        ent.bind('<Return>', lambda _e: self._on_search())
        ent.bind('<KeyRelease>', lambda _e: self._on_search())

        ttk.Button(top, text='刷新', width=6, command=self.reload).pack(
            side='right')
        self._count_label = ttk.Label(top, text='', font=F_HINT, foreground=HINT)
        self._count_label.pack(side='right', padx=(0, 10))

        # ---- 中间：列表（可滚动）----
        wrap = ttk.Frame(self)
        wrap.grid(row=1, column=0, sticky='nsew', padx=8)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        self._canvas = tk.Canvas(wrap, bg='white', highlightthickness=0, height=300)
        self._canvas.grid(row=0, column=0, sticky='nsew')
        sb = ttk.Scrollbar(wrap, orient='vertical', command=self._canvas.yview)
        sb.grid(row=0, column=1, sticky='ns')
        self._canvas.configure(yscrollcommand=sb.set)

        self._list = tk.Frame(self._canvas, bg='white')
        self._list_id = self._canvas.create_window((0, 0), window=self._list,
                                                   anchor='nw')
        self._list.bind('<Configure>', lambda _e: self._canvas.configure(
            scrollregion=self._canvas.bbox('all')))
        self._canvas.bind('<Configure>', lambda e: self._canvas.itemconfigure(
            self._list_id, width=e.width))
        # 鼠标滚轮（Windows）
        self._canvas.bind_all('<MouseWheel>', self._on_wheel, add='+')

        # ---- 底部：更多 ----
        bottom = ttk.Frame(self)
        bottom.grid(row=2, column=0, sticky='ew', padx=8, pady=(6, 0))
        self._more_btn = ttk.Button(bottom, text='显示更多', command=self._more)
        self._more_btn.pack(side='left')

    def _on_wheel(self, event: tk.Event) -> None:
        try:
            if self.winfo_exists() and self._canvas.winfo_exists():
                self._canvas.yview_scroll(int(-event.delta / 120), 'units')
        except Exception:
            pass

    # ---------------- 数据 ----------------

    def set_kind(self, kind: str) -> None:
        self._kind = kind
        self._limit = PAGE_SIZE
        self.reload()

    def _on_search(self) -> None:
        kw = self._search_var.get().strip()
        if kw != self._keyword:
            self._keyword = kw
            self._limit = PAGE_SIZE
            self.reload()

    def _more(self) -> None:
        self._limit += PAGE_SIZE
        self.reload()

    def reload(self) -> None:
        """重新读一遍归档并重画列表。"""
        for w in self._rows:
            try:
                w.destroy()
            except Exception:
                pass
        self._rows = []

        if H is None:
            self._count_label.configure(text='历史模块不可用')
            return

        try:
            entries = H.load(limit=self._limit, kind=self._kind,
                             keyword=self._keyword)
        except Exception as e:
            logger.warning(f'读取历史失败: {e}')
            entries = []
        self._entries = entries

        total_hint = f'{len(entries)} 条'
        try:
            if not self._kind and not self._keyword:
                d, c = H.count_by_kind(H.load(limit=1000))
                total_hint = f'听写 {d} · 对话 {c}（显示最近 {len(entries)} 条）'
        except Exception:
            pass
        self._count_label.configure(text=total_hint)

        for e in entries:
            self._rows.append(self._make_row(e))

        # 还有更多才显示按钮
        try:
            if len(entries) >= self._limit:
                self._more_btn.pack(side='left')
            else:
                self._more_btn.pack_forget()
        except Exception:
            pass

    def _make_row(self, entry: Any) -> tk.Widget:
        row = tk.Frame(self._list, bg='white')
        row.pack(fill='x', pady=(0, 2))

        head = tk.Frame(row, bg='white')
        head.pack(fill='x')
        tag = '对话' if entry.kind == H.KIND_CHAT else '听写'
        color = ACCENT if entry.kind == H.KIND_CHAT else HINT
        tk.Label(head, text=f'{entry.time_text}  {tag}', font=F_TIME,
                 bg='white', fg=color).pack(side='left')
        if entry.audio_exists:
            tk.Label(head, text='🎵', font=F_TIME, bg='white',
                     fg=color).pack(side='left', padx=(4, 0))

        # 操作按钮（从右往左）
        tk.Button(head, text='删除', font=F_HINT, bd=0, bg='white', fg=BAD,
                  activebackground='white', cursor='hand2',
                  command=lambda e=entry: self._delete(e)).pack(side='right')
        if entry.kind == H.KIND_CHAT and entry.reply:
            tk.Button(head, text='插入', font=F_HINT, bd=0, bg='white', fg=ACCENT,
                      activebackground='white', cursor='hand2',
                      state='normal' if self._insert else 'disabled',
                      command=lambda t=entry.reply: self._do_insert(t)).pack(side='right', padx=(0, 6))
        tk.Button(head, text='复制', font=F_HINT, bd=0, bg='white', fg=ACCENT,
                  activebackground='white', cursor='hand2',
                  command=lambda t=(entry.reply or entry.text): self._copy(t)).pack(side='right', padx=(0, 6))
        if entry.audio_exists:
            tk.Button(head, text='播放', font=F_HINT, bd=0, bg='white', fg=ACCENT,
                      activebackground='white', cursor='hand2',
                      command=lambda a=entry.audio: self._play(a)).pack(side='right', padx=(0, 6))

        body = entry.text
        if entry.kind == H.KIND_CHAT and entry.reply:
            body = f'{entry.text}\n→ {entry.reply}'
        tk.Label(row, text=body, font=F_TEXT, bg='white', fg=INK,
                 wraplength=680, justify='left', anchor='w').pack(
            fill='x', padx=(2, 0))

        tk.Frame(self._list, bg='#eeeeee', height=1).pack(fill='x', pady=(4, 4))
        return row

    # ---------------- 操作 ----------------

    def _copy(self, text: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            logger.info(f'历史：已复制 {len(text)} 字')
        except Exception as e:
            logger.debug(f'复制失败: {e}')

    def _do_insert(self, text: str) -> None:
        if not self._insert:
            return
        try:
            self._insert(text)
        except Exception as e:
            logger.warning(f'插入历史文本失败: {e}')

    def _play(self, audio: Any) -> None:
        try:
            os.startfile(str(audio))          # noqa: S606 (Windows only)
        except Exception as e:
            logger.debug(f'打开音频失败: {e}')

    def _delete(self, entry: Any) -> None:
        preview = entry.text[:24] + ('…' if len(entry.text) > 24 else '')
        try:
            yes = messagebox.askyesno(
                '删除这条记录',
                f'要删除这条记录吗？\n\n{entry.time_text}  {preview}\n\n'
                '（连同它的原始录音一起删掉，删了就找不回来了）',
                parent=self)
        except Exception:
            yes = False
        if not yes:
            return
        try:
            ok = H.delete(entry) if H else False
        except Exception as e:
            logger.warning(f'删除历史失败: {e}')
            ok = False
        if ok:
            logger.info('历史：删除了一条记录')
            self.reload()
        else:
            try:
                messagebox.showwarning('删除失败', '没能删掉这条记录，详见日志。',
                                       parent=self)
            except Exception:
                pass
