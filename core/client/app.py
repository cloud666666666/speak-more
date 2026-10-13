# coding: utf-8
"""
CapsWriter Offline 客户端主程序门面类 (Facade)

采用外观模式统一管理音频流 (AudioStreamManager)、
识别结果处理 (ResultProcessor) 和快捷键管理 (ShortcutManager)。
"""

import os
import sys
import asyncio
from pathlib import Path

from .state import ClientState
from . import logger
from config_client import ClientConfig as Config, __version__
from core.tools.signal_handler import register_signal
from .state import console
from .connection import WebSocketManager
from typing import TYPE_CHECKING, Optional
from .manager import (
    TrayManager,
    MicRunner, FileRunner
)
from .audio.stream import AudioStreamManager
from .shortcut.shortcut_manager import ShortcutManager
from .shortcut.shortcut_config import Shortcut

from .udp.udp_control import UDPController

from .hotword.manager import HotwordManager
from .llm.llm_handler import LLMHandler
from .output.text_output import TextOutput
from .diary.diary_writer import DiaryWriter
from core.tools.empty_working_set import empty_current_working_set
from platform import system


# 单实例互斥体句柄。必须留一个模块级引用持有到进程结束 ——
# 一旦被 GC 关掉，互斥体就释放了，后面的实例又能进来。
_SINGLETON_MUTEX = None


def _acquire_single_instance(name: str = 'SpeakMore.Client.Singleton') -> bool:
    """
    [补丁] 单实例保护：返回 True 表示本进程拿到了锁，False 表示已有一个在跑。

    为什么需要：客户端会注册全局键盘/鼠标钩子（默认 CapsLock + 鼠标侧键）。
    双击两次图标就是两份钩子抢同一个键 —— 表现为按键时灵时不灵、录音状态
    互相打架，而且第二个实例的托盘图标会跟第一个叠在一起，用户根本分不清。

    用 Windows 命名互斥体（Local\\ 作用域，只在本用户会话内生效）。
    任何异常都返回 True：宁可少一层保护，也不能因此起不来。
    """
    global _SINGLETON_MUTEX
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]

        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return True                        # 建不出来就不挡

        ERROR_ALREADY_EXISTS = 183
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False

        _SINGLETON_MUTEX = handle              # 持有到进程退出
        return True
    except Exception:
        return True


def _warn_already_running() -> None:
    """已经有一个实例在跑时，弹个系统消息框说明怎么办（控制台可能已隐藏）。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            'Speak More 已经在运行了，本窗口会关闭。\n\n'
            '请到任务栏右下角的托盘图标里操作；\n'
            '如果看不到图标，点一下那个「^」把隐藏的图标展开。',
            'Speak More', 0x40)               # MB_ICONINFORMATION
    except Exception:
        pass



class CapsWriterClient:
    """
    CapsWriter 客户端门面类
    
    管理的外部接口简洁：start()。
    """
    def __init__(self):
        # 确保正确的工作目录
        self.base_dir = Path(__file__).parents[2]
        os.chdir(self.base_dir)
            
        # 初始化事件循环
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
            
        # 初始化状态容器
        self.state = ClientState(app=self)

        # 初始化热词管理器
        self.hotword = HotwordManager(
            hotword_files=None,
            threshold=Config.hot_thresh,
            similar_threshold=Config.hot_similar
        )

        # 4. 初始化 LLM 润色系统
        self.llm = LLMHandler(app=self)
        
        self.output = TextOutput()
        self.diary = DiaryWriter(base_path=self.base_dir)

        # 初始化各管理器
        self.ws = WebSocketManager(self)
        self.tray = TrayManager(self)

        # 实例化硬件资源管理组件
        self.stream = AudioStreamManager(self)
        self.shortcut = ShortcutManager(self, [Shortcut(**sc) for sc in Config.shortcuts])
        self.udp = UDPController(self.shortcut)

        # 内存清理
        empty_current_working_set()

    def stop(self):
        """
        统一释放所有资源（清理顺序：硬件 -> 托盘 -> WebSocket -> State）
        """
        logger.info("正在执行 CapsWriterClient 资源释放...")

        # 1. 停止核心运行组件
        self.udp.stop()
        self.shortcut.stop()
        self.stream.stop()

        # 2. 托盘资源
        self.tray.stop()

        # 3. 关闭监控
        self.hotword.stop()
        self.llm.stop()

        # 4. 关闭 WebSocket 连接
        self.ws.close_sync()

        # 5. 重置 State
        try:
            self.state.reset()
        except Exception as e:
            logger.warning(f"重置状态时发生错误: {e}")

        # 6. 停止事件循环（最后一步，确保前面的异步操作已调度）
        self.loop.stop()

        # [补丁] 关掉录音浮层的 Tk 线程
        try:
            from core.ui.recording_overlay import stop_overlay
            stop_overlay()
        except Exception as _e:
            logger.debug(f'关闭录音浮层时出错: {_e}')

        # 7. [补丁] 把服务端一起收掉
        #
        # 服务端的托盘图标已经被补丁关掉了（config_local.PATCH_SERVER_DEFAULTS），
        # 不在这里收掉的话它会变成一个看不见、却还占着 1.6GB 内存 / 1.7GB 显存
        # 的孤儿进程，用户除了任务管理器没别的办法弄掉它。
        try:
            from .server_lifecycle import stop as _stop_server
            _stop_server(timeout=5.0)
        except Exception as _e:
            logger.warning(f'停止服务端时出错（不影响客户端退出）: {_e}')

        logger.info("资源释放完成")
        console.print('[green4]再见！')


    def start(self):
        """
        启动客户端 (唯一入口)
        
        自动根据命令行参数识别模式。内部管理异步循环。
        """

        # [补丁] 单实例保护：必须在注册全局快捷键、启托盘之前拦下来 ——
        # 两份键盘钩子已经开始互相抢键了再退就晚了。
        if not _acquire_single_instance():
            logger.warning('已有一个 Speak More 在运行，本实例直接退出')
            _warn_already_running()
            return

        # 注册退出函数
        register_signal(self.stop)

        # [补丁] 应用图标统一（core/ui/app_icon.py）：
        #   ① 任务栏身份设成 SpeakMore.Client —— 不设的话 Windows 按**进程
        #      可执行文件**归类，也就是 python.exe，任务栏会拿 Python 的图标
        #      顶替，pin 到任务栏之后更是永远一个 Python 图标；
        #   ② 黑窗口那个 python 图标换掉（托盘的「👁️ 显示/隐藏」会把它亮出来）。
        # **必须在建任何窗口之前**，所以放在最前面。
        try:
            from core.ui.app_icon import apply_everything as _apply_icons
            _apply_icons()
        except Exception as _e:
            logger.debug(f'应用图标设置失败（不影响使用）: {_e}')

        # [补丁] 启动预检：模型与 llama.cpp 运行库齐不齐。
        #
        # 为什么必须在这里说清楚：服务端是被我们用隐藏控制台 + stdin=DEVNULL
        # 拉起来的，缺模型时它只会静默退出（日志一行、界面什么都没有），
        # 用户的体验是"双击了、托盘也在、按住 CapsLock 毫无反应"。
        # 放在拉起服务端之前 —— 先告诉用户为什么，再让服务端去撞墙。
        try:
            from .model_precheck import run_precheck, notify_if_problems
            notify_if_problems(run_precheck(self.base_dir), logger)
        except Exception as _e:
            logger.debug(f'启动预检失败（不影响启动）: {_e}')

        # [补丁] 按保留策略清理历史记录（用户在「记录」页选过"只留 7 天"之类）。
        # 放在启动时做，而不是每条识别后做 —— 清理是低频操作，不该掺进热路径。
        try:
            from . import history as _history
            _removed, _days = _history.purge(getattr(Config, 'history_keep', 'forever'))
            if _removed:
                logger.info(f'历史保留策略：清理了 {_removed} 条旧记录（涉及 {_days} 天）')
        except Exception as _e:
            logger.debug(f'历史清理失败（不影响启动）: {_e}')

        # [补丁] 确保服务端在跑（不在就后台拉起来）
        #
        # 用异步版本：ResultProcessor.start() 本身就是每 2 秒重试的连接循环
        # （result_processor.py:159-165），不需要在这里阻塞等端口就绪，
        # 托盘图标也能立刻出现。
        try:
            from .server_lifecycle import ensure_async as _ensure_server_async
            _ensure_server_async()
        except Exception as _e:
            logger.warning(f'自动拉起服务端失败（可手动启动服务端）: {_e}')

        # [补丁] 录音状态浮层（屏幕下方那条跟着声音跳的波形）
        try:
            from core.ui.recording_overlay import start_overlay
            start_overlay(self)
        except Exception as _e:
            logger.warning(f'录音浮层启动失败（不影响录音）: {_e}')

        # [补丁] 第一次启动时弹首次设置向导（延迟几秒等托盘先出来）。
        # 已经看过一次就不再弹，标记写在 config_local.json 的 meta.wizard_done。
        try:
            from core.ui.settings_handler import on_maybe_show_wizard
            on_maybe_show_wizard(self)
        except Exception as _e:
            logger.warning(f'首次向导未能启动: {_e}')

        files = [Path(f) for f in sys.argv[1:] if os.path.exists(f)]

        if files:
            # 文件转录模式
            runner = FileRunner(self, files)
        else:
            # 麦克风实时模式
            runner = MicRunner(self)
        
        try:
            self.loop.run_until_complete(runner.run())
        except RuntimeError:
            ...


