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


