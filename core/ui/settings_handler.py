# coding: utf-8
"""
设置面板的线程管理与保存编排

线程模型（照抄 core/ui/context_menu_handler.py 的模式，但修掉它两个疤）：

    托盘回调 on_open_settings()   <- 运行在 pystray 的消息线程
        └─ 只 set 一个标志 + 起 worker 线程，**立即返回**

    worker 线程
        ├─ build_snapshot(app)          读 Config / app.llm.roles（纯数据）
        ├─ manager.show_dialog(snapshot) 阻塞在本线程，等 Tk 线程出结果
        └─ settings_apply.apply_result(app, data, before)   落盘 + 生效

    独立的 Tk 线程（_SettingsDialogManager 自己起的 daemon）
        └─ tk.Tk() + mainloop() + after(100, 轮询请求队列)

修的疤①：**不再阻塞 pystray 线程**。作者的 on_edit_context / on_add_hotword
    直接在托盘回调里做 result_queue.get(timeout=300)，那 5 分钟里托盘菜单
    完全点不动（连「退出」都不行）。设置面板用户会开着慢填，必须避开。

修的疤②：超时从 300 秒提到 1800 秒。填 API Key、翻文档、试连远超 5 分钟。

数据边界：**app 对象绝不进 Tk 线程**。传给对话框的快照全是纯数据。
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from typing import Any, Dict, List, Optional

from config_client import ClientConfig as Config
from . import logger


# 角色字段：schema 里以 'llm.' 开头的那些，去掉前缀就是 RoleConfig 的字段名
_ROLE_ATTRS: List[str] = []


def _role_attrs() -> List[str]:
    global _ROLE_ATTRS
    if not _ROLE_ATTRS:
        from core.ui import settings_schema as S
        _ROLE_ATTRS = sorted({
            S.role_attr(f.key) for f in S.all_fields() if S.is_role_field(f.key)
        })
    return _ROLE_ATTRS


# ============================================================
#  托盘回调
# ============================================================

_dialog_manager: Optional['_SettingsDialogManager'] = None
_manager_lock = threading.Lock()
_busy = threading.Event()          # 防重入：面板已经开着就别再开一个


def get_settings_manager() -> '_SettingsDialogManager':
    global _dialog_manager
    with _manager_lock:
        if _dialog_manager is None:
            _dialog_manager = _SettingsDialogManager()
        return _dialog_manager


def on_open_settings(app: Any) -> None:
    """
    托盘菜单「⚙️ 设置」的回调（也是左键单击托盘图标的默认动作）。

    **运行在 pystray 的消息线程，本函数必须立即返回** —— 真正的活儿
    全丢给 worker 线程，否则面板一开着托盘菜单就整个卡死。

    重复点击的行为：**把已经开着的那个面板拉到前台**，而不是再开一个、
    也不弹"已经打开了"的提示。用户点几次就该看到一个，这是正常软件的直觉。
    """
    if _busy.is_set():
        logger.info('设置面板请求：已有面板/请求在处理中，改为聚焦已有窗口')
        try:
            if get_settings_manager().focus_existing():
                return
        except Exception as e:
            logger.debug(f'聚焦已有设置面板失败: {e}')
        # 面板正在创建、句柄还没拿到时会走到这里。
        # 静默返回就好 —— 弹一个无边框、没有关闭按钮的提示反而更烦人。
        logger.debug('设置面板正在打开中，忽略这次点击')
        return

    logger.info('设置面板请求：受理，开始后台打开')
    _busy.set()
    threading.Thread(target=_worker, args=(app,), daemon=True,
                     name='SettingsOpen').start()


def _worker(app: Any) -> None:
    try:
        before = build_snapshot(app)
        manager = get_settings_manager()
        result = manager.show_dialog(
            before,
            mic_apply=lambda name: _apply_mic_now(app, name),
            key_capture=_make_key_capture(app))

        if result is None or not getattr(result, 'confirmed', False):
            logger.debug('用户取消了设置面板')
            return

        from core.client.settings_apply import apply_result, summarize

        report = apply_result(app, result.data, before)

        logger.info(f'设置面板保存: 生效={report.applied} '
                    f'需重启={report.restart} 错误={report.errors}')

        # 保存成功**不弹任何东西** —— 用户点了保存、窗口关掉，本身就是反馈。
        # 只有这两种情况值得打断他：
        #   ① 出错（否则他以为存上了，其实没有）
        #   ② 有项目得重启才生效（比如改了日志级别）
        if report.errors:
            _mini_toast(summarize(report), duration=9000, error=True)
        elif report.restart:
            _mini_toast(summarize(report), duration=6000)

    except Exception as e:
        logger.error(f'设置面板出错: {e}', exc_info=True)
        _mini_toast(f'设置保存失败: {e}', duration=9000, error=True)
    finally:
        _busy.clear()


def _mini_toast(message: str, duration: int = 4000, error: bool = False) -> None:
    """
    小号提示条。

    为什么不用默认外观：作者那套 toast 默认 `initial_width=0.5` —— **占屏幕宽
    度的一半**，再配上饱和的蓝色，弹出来很扎眼（用户反馈"很不美观"）。
    这里统一收窄到 28%、字号调小、换成低调的深灰。

    而且**只在真正需要打断用户的时候才用**：保存成功这类"我做完了"的反馈
    一律静默 —— 窗口关掉本身就是反馈。
    """
    try:
        from .toast import toast
        toast(message, font_size=11, duration=duration,
              bg='#B91C1C' if error else '#2B3440',
              initial_width=0.28)
    except Exception as e:
        logger.debug(f'toast 显示失败（不影响功能）: {e}')


# ============================================================
#  首次启动向导
# ============================================================

def should_show_wizard() -> bool:
    try:
        import config_local
        return not config_local.is_wizard_done()
    except Exception as e:
        logger.debug(f'判断是否需要首次向导失败（当作不需要）: {e}')
        return False


def on_maybe_show_wizard(app: Any, delay: float = 3.0) -> None:
    """
    客户端启动后调用。只在**第一次**弹向导
    （config_local.json 的 meta.wizard_done 标记没写过时）。

    延迟几秒是为了让托盘图标先出现 —— 一启动就糊用户一脸弹窗体感很差。
    """
    if not should_show_wizard():
        return
    threading.Thread(target=_wizard_worker, args=(app, delay), daemon=True,
                     name='FirstRunWizard').start()


def _make_key_capture(app: Any):
    """
    给设置面板的「按一下键就捕获」造一个入口。

    为什么要绕这一道：捕获时必须先把**宿主 app 的快捷键监听**停下来 ——
    CapsLock 就是默认快捷键、`suppress=True`，应用的低级钩子会先把它吃掉，
    捕获器什么都收不到，用户还会看到录音浮层莫名其妙地冒出来。
    而停监听需要 `app` 对象，对话框那边（刻意）拿不到，所以从这里注入。

    真正的活儿在 core/ui/key_capture.py，这里只负责把 app 绑进去。
    """
    def _capture(timeout: float = 15.0, cancel: Any = None) -> Any:
        from core.ui.key_capture import capture_next_key
        return capture_next_key(app=app, timeout=timeout, cancel=cancel)

    return _capture


def _apply_mic_now(app: Any, device_name: str) -> bool:
    """
    把麦克风选择**立刻**应用上去：改内存配置 + 重开音频流。

    向导的「测试麦克风」在测之前会调它。不这么做的话，用户在向导里换了个
    设备再点测试，看到的其实是**旧设备**的电平 —— 等于没测，而且他还会
    以为新设备是好的。

    Returns:
        True 表示音频流确实用新设置重新打开了。
    """
    try:
        from config_client import ClientConfig as Config
        Config.mic_device = device_name
    except Exception as e:
        logger.warning(f'写入麦克风配置失败: {e}')
        return False

    if getattr(app.state, 'recording', False):
        logger.info('正在录音，跳过滤层切换')
        return False

    try:
        stream = app.stream.reopen()
    except Exception as e:
        logger.warning(f'重开音频流失败（设备 {device_name!r}）: {e}')
        return False

    if stream is None:
        # reopen() 内部把 PortAudioError 吃掉了并返回 None，这里要认出来，
        # 否则会告诉用户"测试通过"而其实音频流压根没起来
        logger.warning(f'音频流没能用设备 {device_name!r} 打开')
        return False

    logger.info(f'向导测试麦克风：已切换到 {device_name or "系统默认"}')
    return True


def _wizard_worker(app: Any, delay: float) -> None:
    try:
        time.sleep(max(0.0, delay))

        before = build_snapshot(app)
        manager = get_settings_manager()
        result = manager.show_wizard(before, mic_apply=lambda name: _apply_mic_now(app, name))

        if result is None or not getattr(result, 'confirmed', False):
            logger.debug('用户取消了首次向导')
            return

        from core.client.settings_apply import apply_result, summarize
        report = apply_result(app, result.data, before)

        # 无论用户改没改，都标记"看过向导了"，不重复弹
        try:
            import config_local
            config_local.mark_wizard_done()
        except Exception as e:
            logger.warning(f'标记首次向导已完成失败（下次可能还会弹）: {e}')

        logger.info(f'首次向导完成: 生效={report.applied} '
                    f'需重启={report.restart} 错误={report.errors}')

        # 向导里如果改到了需要重启的项才提示；正常完成不弹任何东西
        # （向导最后一页已经用大白话把设置总结过一遍了）
        if report.errors:
            _mini_toast(summarize(report), duration=9000, error=True)
        elif report.restart:
            _mini_toast(summarize(report), duration=6000)

    except Exception as e:
        logger.error(f'首次向导出错: {e}', exc_info=True)


# ============================================================
#  快照：app -> 纯数据
# ============================================================

def build_snapshot(app: Any) -> Dict[str, Any]:
    """把当前配置读成一份纯数据快照。在 worker 线程里调用，不碰 Tk。"""
    from core.ui import settings_schema as S

    client: Dict[str, Any] = {}
    for f in S.all_fields():
        if f.key == 'shortcuts':
            continue
        if S.is_role_field(f.key):
            continue
        client[f.key] = getattr(Config, f.key, f.default)

    client['shortcuts'] = [
        dict(sc) for sc in (getattr(Config, 'shortcuts', None) or [])
        if isinstance(sc, dict)
    ]

    # 记一行：界面和配置"对不上"的争论，看这行就能定案
    # （面板是按这份快照画的，快照就是这一行里记的内容）
    try:
        parts = [
            f"{s.get('key')}({'按住' if s.get('hold_mode', True) else '点按'}"
            f"{'' if s.get('enabled', True) else '/禁用'})"
            for s in client['shortcuts']
        ]
        logger.info('设置面板快照：快捷键 = ' + ('、'.join(parts) or '（空）'))
    except Exception:
        pass

    # ---- LLM 角色 ----
    roles: Dict[str, Dict[str, Any]] = {}
    llm = getattr(app, 'llm', None)
    if llm is not None:
        try:
            for name, rc in (getattr(llm, 'roles', {}) or {}).items():
                roles[str(name)] = {
                    attr: getattr(rc, attr, None)
                    for attr in _role_attrs() if hasattr(rc, attr)
                }
        except Exception as e:
            logger.warning(f'读取 LLM 角色失败: {e}')

    names = list(roles.keys())
    if '默认' in names:                       # 「默认」置顶
        names.remove('默认')
        names.insert(0, '默认')

    return {
        'client': client,
        'llm_roles': roles,
        'role_names': names or ['默认'],
        'current_role': names[0] if names else '默认',
        'devices': _device_choices(),
        'hotwords': _hotword_snapshot(app),
        'meta': _meta_text(),
    }


def _hotword_snapshot(app: Any) -> Dict[str, Any]:
    """
    读热词表原文给「热词」页用。

    `manual` 为 **None**（不是空串）表示读不到 —— 对话框看到 None 会把那
    一页标成"暂时不可用"，而不是显示一个空编辑框（显示空框的话用户一保存
    就把磁盘上的 hot.txt 清空了）。
    """
    try:
        from core.client.hotword import hot_file
    except Exception as e:
        logger.debug(f'热词文件模块不可用: {e}')
        return {}

    try:
        base = None
        try:
            base = app.hotword.files.get('hot')
        except Exception:
            pass

        manual = hot_file.read_hot(base)
        if manual is None:
            logger.warning(f'读不到热词表 {hot_file.hot_path(base)}')
            return {}
        logger.info(f'热词快照：hot.txt {len(manual)} 字符')
        return {'manual': manual}
    except Exception as e:
        logger.warning(f'读取热词失败: {e}')
        return {}


def _device_choices() -> List[tuple]:
    out: List[tuple] = [('系统默认（跟随系统设置）', '')]
    try:
        from core.client.audio.device_select import list_input_devices
        seen = set()
        for _idx, name, _ch in list_input_devices():
            if name in seen:
                continue
            seen.add(name)
            out.append((name, name))
    except Exception as e:
        logger.debug(f'枚举麦克风失败（不影响其它设置）: {e}')
    return out


def _meta_text() -> str:
    """
    底部状态栏那一行。

    **必须短。** 这里踩过一个大坑：原来把 status_summary() 整个塞进来
    （85 个字符："补丁 v0.1.0 · 基础版本 ? · 写入者 settings-panel ·
    客户端覆盖 32 项 · 角色覆盖 4 个"），它和右边的「取消 / 保存」在同一行，
    把整行宽度吃光，两个按钮被挤成 **1 像素宽** —— 看得见字样但点不到，
    用户改完设置根本保存不了，还以为是功能坏了。

    详细的覆盖信息挪到「关于」页去显示了。
    """
    try:
        from core.branding import APP_NAME
    except Exception:
        APP_NAME = 'CapsWriter'

    try:
        import config_client
        version = getattr(config_client, '__version__', '?')
    except Exception:
        version = '?'

    try:
        from config_local import PATCH_VERSION
        patch = f' · 补丁 v{PATCH_VERSION}'
    except Exception:
        patch = ''

    return f'{APP_NAME} {version}{patch}'


# ============================================================
#  对话框管理器（Tk 跑在自己的 daemon 线程里）
# ============================================================

class _SettingsDialogManager:
    """与 _ContextDialogManager 同构，但超时 1800 秒。"""

    def __init__(self):
        self.request_queue: 'queue.Queue[Dict[str, Any]]' = queue.Queue()
        self.result_queue: 'queue.Queue[Any]' = queue.Queue()
        self.root: Optional[tk.Tk] = None
        self.is_running = False
        # 当前开着的那个设置面板。重复点击时用它把窗口拉到前台，
        # 而不是再开一个。见 focus_existing()。
        self._current_dialog: Optional[Any] = None
        # 有没有对话框正在显示中。用来挡住嵌套 —— wait_window 开的是
        # 嵌套事件循环，不挡的话请求会一层层叠出多个对话框。
        self._dialog_open = False

        self.tk_thread = threading.Thread(
            target=self._run_tkinter, daemon=True, name='SettingsDialogThread')
        self.tk_thread.start()
        self._wait_for_ready()

    def _wait_for_ready(self, timeout: float = 5.0) -> None:
        import time
        start = time.time()
        while not self.is_running and (time.time() - start) < timeout:
            time.sleep(0.05)
        if not self.is_running:
            raise RuntimeError('设置面板的 Tkinter 线程启动超时')

    def _run_tkinter(self) -> None:
        try:
            self.root = tk.Tk()
            self.root.withdraw()
            self.root.protocol('WM_DELETE_WINDOW', self._on_close)
            self.is_running = True
            self._process_requests()
            self.root.mainloop()
        except Exception as e:
            logger.error(f'设置面板 Tkinter 线程异常: {e}', exc_info=True)
            self.is_running = False

    def _on_close(self) -> None:
        self.is_running = False
        if self.root:
            try:
                self.root.quit()
            except Exception:
                pass

    def _process_requests(self) -> None:
        if not self.is_running:
            return
        try:
            if self._dialog_open:
                # 对话框开着的时候来的请求一律回绝。它们要是排在队列里，
                # 会在第一个关掉之后挨个再弹出来 —— 用户看到的是
                # "点一次冒出来好几个"，而且每个 worker 拿到的是哪一次的结果
                # 完全看运气（这正是"改了没生效、还被当成恢复默认"的根源）。
                self._reject_pending()
            else:
                try:
                    request = self.request_queue.get_nowait()
                except queue.Empty:
                    request = None
                if request is not None:
                    self._reject_pending()      # 一次只受理一个
                    self._show_dialog_impl(request)
        except Exception as e:
            logger.error(f'处理对话框请求时出错: {e}', exc_info=True)

        if self.root:
            self.root.after(100, self._process_requests)

    def _reject_pending(self) -> None:
        """把排队的请求全部回绝（回一个 None，让对应的 worker 别死等）。"""
        n = 0
        while True:
            try:
                self.request_queue.get_nowait()
            except queue.Empty:
                break
            self.result_queue.put(None)
            n += 1
        if n:
            logger.info(f'同一时刻只允许一个对话框：已回绝 {n} 个排队的请求')

    def _show_dialog_impl(self, request: Dict[str, Any]) -> None:
        kind = request.get('kind', 'settings')

        # 绝不能叠对话框。
        #
        # `wait_window()` 会开一个**嵌套事件循环**，而 _process_requests 是
        # 每 100ms 用 root.after 轮询的 —— 对话框还开着的时候轮询照样会跑，
        # 于是队列里的下一个请求又会建一个对话框，一层层叠上去。
        # 用户看到的是"点一次冒好几个"，而每次关闭都会往 result_queue 里塞
        # 一个结果，worker 拿到的是哪个完全看运气 ——
        # 这就是"改了半天没生效、还被当成恢复默认"的根源。
        if self._dialog_open:
            logger.warning(f'已有对话框在显示中，忽略这次请求（{kind}）')
            self.result_queue.put(None)
            return

        snapshot = request.get('snapshot') or {}
        self._dialog_open = True
        try:
            if kind == 'wizard':
                from core.ui.first_run_wizard import FirstRunWizard
                dialog: Any = FirstRunWizard(self.root, request.get('mic_apply'))
            else:
                from core.ui.settings_dialog import SettingsDialog
                # 设置面板也要 mic_apply —— 麦克风那一栏有「测试麦克风」，
                # 测之前必须先把选中的设备应用上去（否则测的是旧设备）
                dialog = SettingsDialog(self.root, request.get('mic_apply'),
                                        request.get('key_capture'))

            # 留住实例引用，好让 focus_existing() 能把它拉到前台
            self._current_dialog = dialog
            try:
                result = dialog.show(snapshot)
            finally:
                self._current_dialog = None

            self.result_queue.put(result)
        except Exception as e:
            logger.error(f'显示对话框时出错（{kind}）: {e}', exc_info=True)
            self._current_dialog = None
            self.result_queue.put(None)
        finally:
            self._dialog_open = False

    def focus_existing(self) -> bool:
        """
        把已经开着的设置面板拉到前台。

        Returns:
            True 表示确实有一个窗口被拉到前台；False 表示当前没有面板
            （比如还在创建中，或者已经被用户关掉了）。
        """
        dialog = self._current_dialog
        window = getattr(dialog, '_dialog', None) if dialog is not None else None
        if window is None or self.root is None:
            return False

        def _raise() -> None:
            try:
                if not window.winfo_exists():
                    return
                window.deiconify()
                # Windows 会拦掉后台进程的 SetForegroundWindow，
                # 临时置顶一下是业界通用的绕过办法
                window.lift()
                window.attributes('-topmost', True)
                window.focus_force()
                window.after(300, lambda: _unpin(window))
            except Exception as e:
                logger.debug(f'拉前设置面板失败: {e}')

        def _unpin(win: Any) -> None:
            try:
                win.attributes('-topmost', False)
            except Exception:
                pass

        try:
            self.root.after(0, _raise)
            return True
        except Exception:
            return False

    def show_dialog(self, snapshot: Dict[str, Any], timeout: float = 1800,
                    mic_apply: Any = None, key_capture: Any = None) -> Any:
        """把快照丢给 Tk 线程，阻塞等待用户点保存/取消。"""
        return self._show('settings', snapshot, timeout, mic_apply, key_capture)

    def show_wizard(self, snapshot: Dict[str, Any], timeout: float = 1800,
                    mic_apply: Any = None, key_capture: Any = None) -> Any:
        """首次启动向导走同一条 Tk 线程，省得为了它再起一个 Tk 实例。"""
        return self._show('wizard', snapshot, timeout, mic_apply, key_capture)

    def _show(self, kind: str, snapshot: Dict[str, Any], timeout: float,
              mic_apply: Any = None, key_capture: Any = None) -> Any:
        # 先把上一次可能残留的结果丢掉。
        #
        # result_queue 是普通的 Queue，如果某个请求因为重试/超时留下了没人取
        # 的结果，下一次 _show 会**立刻拿到旧结果**返回 —— worker 会以为
        # "用户已经操作完了"，在对话框还开着的时候就去保存，
        # 而且保存的是上一次的数据。清干净再排队。
        drained = 0
        while True:
            try:
                self.result_queue.get_nowait()
                drained += 1
            except queue.Empty:
                break
        if drained:
            logger.debug(f'清掉了 {drained} 个残留的对话框结果')

        payload: Dict[str, Any] = {'kind': kind, 'snapshot': snapshot}
        if mic_apply is not None:
            payload['mic_apply'] = mic_apply
        if key_capture is not None:
            payload['key_capture'] = key_capture
        self.request_queue.put(payload)
        logger.debug(f'对话框请求已排队（{kind}）')
        try:
            return self.result_queue.get(timeout=timeout)
        except queue.Empty:
            logger.error(f'对话框操作超时（{kind}）')
            return None
