# coding: utf-8
import os
from . import logger
import os, sys, subprocess, time
from config_client import ClientConfig as Config


class TrayManager:
    """
    托盘管理器：负责系统托盘图标的初始化、菜单构建及回调处理。
    """
    def __init__(self, app):
        self.app = app

    @property
    def state(self):
        return self.app.state

    def start(self):
        """初始化系统托盘图标"""
        if not Config.enable_tray:
            return

        try:
            from ..ui import enable_min_to_tray
        except ImportError as e:
            logger.warning(f"托盘模块导入失败，跳过托盘功能: {e}")
            return

        # 获取图标路径
        icon_path = os.path.join(self.app.base_dir, 'assets', 'icon.ico')
        
        # 启用托盘
        # [补丁] 应用对外显示名（本分发版叫 Speak More）
        try:
            from core.branding import TRAY_TITLE as _tray_title
        except Exception:
            _tray_title = 'CapsWriter Client'

        enable_min_to_tray(
            _tray_title,
            icon_path,
            exit_callback=self.app.stop,
            more_options=[
                # [补丁] 图形设置面板。放在最前面是因为它是最常用的入口
                ('⚙️ 设置', self._open_settings),
                ('📋 复制结果', self._copy_last_result),
                ('📁 日记', self._open_diary),
                ('📝 上下文', self._add_context),
                ('✨ 热词', self._add_hotword),
                ('🧹 清除记忆', self._clear_memory),
                ('♻️ 重开音频', self._restart_audio),
                # [补丁] 服务端的托盘图标已关掉，控制项并到这里来
                ('🔌 重启服务端', self._restart_server),
            ],
            # [补丁] 左键单击托盘图标 -> 打开设置面板。
            # 作者原本把「显示/隐藏」设成默认动作，左键单击会把隐藏的
            # 控制台窗口弹出来，用户看到的是一个莫名其妙的 cmd 黑框。
            default_option='⚙️ 设置',
        )
        logger.info("托盘图标已启用")

    def stop(self):
        """停止托盘图标"""
        if not Config.enable_tray:
            return
            
        try:
            from ..ui import stop_tray
            stop_tray()
            logger.info("TrayManager: 托盘图标已卸载")
        except Exception as e:
            logger.debug(f"TrayManager: 卸载托盘时发生错误: {e}")

    def _open_settings(self):
        """
        [补丁] 打开图形设置面板

        注意：本回调**运行在 pystray 的消息线程**上。on_open_settings()
        内部只会起一个 worker 线程就立刻返回，所以这里不会卡住托盘菜单
        （作者原有的 _add_context 是直接在托盘线程里等对话框结果的，
        面板开着的时候托盘会整个点不动，所以我们没那么做）。
        """
        try:
            from core.ui.settings_handler import on_open_settings
            on_open_settings(self.app)
        except Exception as e:
            logger.error(f"打开设置面板失败: {e}", exc_info=True)

    def _restart_server(self):
        """
        [补丁] 重启识别服务端

        服务端自己的托盘图标已经被补丁关掉了（config_local 的
        PATCH_SERVER_DEFAULTS），所以它的生命周期控制都并到客户端菜单里：
        客户端负责在启动时拉起它、退出时收掉它，这里负责手动重启。

        异步执行 —— 服务端要重新加载模型（约 5~10 秒），
        不能把 pystray 的消息线程卡住。
        """
        try:
            from ..server_lifecycle import restart_async

            from ..ui import toast
            # 用收窄的小提示条。作者那套 toast 默认 initial_width=0.5，
            # 占屏幕宽度一半、还配饱和蓝色，弹出来太扎眼（用户反馈过"不美观"）。
            toast('正在重启服务端…（重新加载模型约需 10 秒）',
                  font_size=11, duration=5000, bg='#2B3440', initial_width=0.28)
            restart_async()
            logger.info("用户从托盘请求重启服务端")
        except Exception as e:
            logger.error(f"重启服务端失败: {e}", exc_info=True)

    def _restart_audio(self):
        """重启音频流回调"""
        if hasattr(self.app, 'stream') and self.app.stream:
            self.app.stream.reopen()
            logger.info("用户请求重启音频")

    def _clear_memory(self):
        """清除 LLM 对话历史回调"""
        from ..ui import toast
        if self.app.llm:
            self.app.llm.clear_history()
            toast("清除成功：已清除所有角色的对话历史记录",
                  font_size=11, duration=3000, bg='#2B3440', initial_width=0.28)

    def _open_diary(self):
        """用系统默认方式打开当月日记文件夹回调"""
        base = self.app.diary.base_path
        folder = base / time.strftime('%Y') / time.strftime('%m')
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == 'win32':
            os.startfile(folder)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', folder])
        else:
            subprocess.Popen(['xdg-open', folder])

    def _add_hotword(self):
        """用系统默认方式打开热词文件回调"""
        
        target = os.path.abspath('hot.txt')
        if sys.platform == 'win32':
            os.startfile(target)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', target])
        else:
            subprocess.Popen(['xdg-open', target])

    def _add_context(self):
        """打开编辑上下文界面回调"""
        try:
            from ..ui import on_edit_context
            on_edit_context()
        except ImportError as e:
            logger.warning(f"无法导入上下文菜单处理器: {e}")

    def _copy_last_result(self):
        """复制最后一次识别结果到剪贴板回调"""
        text = self.state.last_output_text
        if text:
            from ..llm.llm_clipboard import copy_to_clipboard
            copy_to_clipboard(text)

    def _request_exit(self, icon=None, item=None):
        """托盘图标引用的退出回调"""
        logger.info("托盘退出: 用户点击退出菜单，准备清理资源并退出")
        self.app.stop()
