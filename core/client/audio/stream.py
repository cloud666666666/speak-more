# coding: utf-8
"""
音频流管理模块

提供 AudioStreamManager 类用于管理音频输入流，包括流的创建、
启动、停止和设备检测。
"""

from __future__ import annotations

import sys
import time
import threading
from typing import TYPE_CHECKING, Optional

import numpy as np
import sounddevice as sd

from core.client.state import console
from . import logger

if TYPE_CHECKING:
    from core.client.state import ClientState
    from ..app import CapsWriterClient



class AudioStreamManager:
    """
    音频流管理器

    负责管理音频输入流的生命周期，包括：
    - 检测和选择音频设备
    - 创建和启动音频流
    - 处理音频数据回调
    - 流的重启和关闭

    Attributes:
        state: 客户端状态实例
        sample_rate: 采样率（默认 48000Hz）
        block_duration: 每个数据块的时长（秒，默认 0.05s）
    """

    SAMPLE_RATE = 48000
    BLOCK_DURATION = 0.05  # 50ms
    IDLE_CHECK_INTERVAL = 1.0  # [补丁] 空闲看门狗多久查一次（秒）

    def __init__(self, app: CapsWriterClient):
        """
        初始化音频流管理器

        Args:
            app: 客户端 App 实例
        """
        self.app = app
        self._channels = 1
        self._running = False  # 标志是否应该运行

        # [补丁] 空闲自动关麦。
        #
        # 上游的输入流是"启动就开、一直开"（mic_runner.start_resources 里
        # 一次 start()），所以只要客户端在跑，Windows 任务栏的
        # 「麦克风正在使用」图标就会一直亮 —— 哪怕用户一整天没按过快捷键。
        # 这里加一条看门狗线程：安静够久就把流关掉释放设备，下次录音
        # （task.launch → ensure_running）或设置里「测试麦克风」（reopen）
        # 时再自动打开。开关与时长见 config_local 的 mic_idle_release /
        # mic_idle_close_seconds。
        self._last_active = time.time()   # 最后一次"真的在用麦克风"的时刻
        self._disposed = False            # 退出时置位，看门狗自行退出
        threading.Thread(target=self._watch_idle, daemon=True,
                         name='MicIdleWatch').start()

    # ---------------- 空闲自动关麦 ----------------

    @property
    def idle_close_seconds(self) -> float:
        """空闲多少秒后关流；<= 0 表示一直开着（上游行为）。配置坏了按 60。"""
        try:
            from config_client import ClientConfig as Config
            return float(getattr(Config, 'mic_idle_close_seconds', 60) or 0)
        except Exception:
            return 60.0

    @property
    def idle_release_enabled(self) -> bool:
        """「不用时自动释放麦克风」开关是否生效。"""
        try:
            from config_client import ClientConfig as Config
            if not bool(getattr(Config, 'mic_idle_release', True)):
                return False
            return self.idle_close_seconds > 0
        except Exception:
            return False

    def _held_open(self) -> bool:
        """当前是不是"正用着麦克风"：录音中，或设置向导里正在测麦克风。"""
        try:
            if self.state.recording:
                return True
        except Exception:
            pass
        try:
            from core.ui.recording_overlay import is_test_mode
            return bool(is_test_mode())
        except Exception:
            return False

    def mark_active(self) -> None:
        """记一次"在用麦克风"，把空闲计时重新拨回 0。"""
        self._last_active = time.time()

    def ensure_running(self) -> Optional[sd.InputStream]:
        """
        录音前调用：流被空闲关掉时把它重新打开。

        `task.launch()` 在按下快捷键的第一时间调这里，所以"空闲自动关麦"
        不会让第一句丢掉开头 —— 打开流（PortAudio 约几十毫秒）发生在
        阈值计时（默认 0.3s）之前。
        """
        self.mark_active()
        if self._running and self.state.stream is not None:
            return self.state.stream
        return self.start()

    def _watch_idle(self) -> None:
        """看门狗：没在用麦克风且空闲超过阈值时关掉采集流。"""
        while not self._disposed:
            time.sleep(self.IDLE_CHECK_INTERVAL)
            try:
                if self._disposed or not self._running:
                    continue
                if self._held_open():
                    # 录音中 / 正在测麦克风：一直算"在用"
                    self.mark_active()
                    continue
                if not self.idle_release_enabled:
                    continue
                seconds = self.idle_close_seconds
                idle = time.time() - self._last_active
                if idle < seconds:
                    continue
                logger.info(f'麦克风空闲 {idle:.0f}s（阈值 {seconds:.0f}s），'
                            f'自动关闭音频流（按快捷键会自动重开）')
                self.stop()
            except Exception as e:                      # pragma: no cover
                logger.debug(f'空闲看门狗出错: {e}')

    def dispose(self) -> None:
        """退出时调用：停掉看门狗线程并关闭音频流。"""
        self._disposed = True
        self.stop()

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info,
        status: sd.CallbackFlags
    ) -> None:
        """
        音频数据回调函数

        当音频流接收到新数据时调用，将数据放入异步队列中。
        """
        # [补丁] 先更新电平（录音浮层 + 设置向导的「测麦克风」都读它）。
        #
        # 放在 recording 判断**之前**是有意的：设置向导需要在非录音状态下
        # 也能看到麦克风有没有声音。代价是空闲时也每块算一次 RMS ——
        # 2400 个采样点约 20µs、每秒约 20 次，可以忽略。
        try:
            from core.ui.recording_overlay import publish_audio
            publish_audio(indata)
        except Exception:
            pass

        # 只在录音状态时处理数据
        if not self.state.recording:
            return

        import asyncio

        # 将数据放入队列
        if self.app.loop and self.state.queue_in:
            asyncio.run_coroutine_threadsafe(
                self.state.queue_in.put({
                    'type': 'data',
                    'time': time.time(),
                    'data': indata.copy(),
                }),
                self.app.loop
            )

    def _on_stream_finished(self) -> None:
        """音频流结束回调"""
        if not threading.main_thread().is_alive():
            return
        if not self._running:
            return

        logger.info("音频流意外结束，正在尝试重启...")
        self.reopen()

    def start(self) -> Optional[sd.InputStream]:
        """
        启动音频流

        Returns:
            创建的音频输入流，如果失败返回 None
        """
        if self._running:
            logger.debug("音频流已在运行，跳过启动")
            return self.state.stream

        # 检测音频设备
        try:
            # [补丁] 按 config_local.json 里配的麦克风设备解析。
            # 没配置时 resolve_input_device() 直接返回系统默认设备，
            # 行为和原版完全一致；配置了但设备不在时回落默认并打 WARNING。
            from core.client.audio.device_select import resolve_input_device
            self._device_index, device = resolve_input_device()
            self._channels = min(2, device['max_input_channels'])
            device_name = device.get('name', '未知设备')
            console.print(
                f'使用默认音频设备：[italic]{device_name}，声道数：{self._channels}',
                end='\n\n'
            )
            logger.info(f"找到音频设备: {device_name}, 声道数: {self._channels}")
        except UnicodeDecodeError:
            logger.warning("无法获取音频设备名称（编码问题）")
        except sd.PortAudioError:
            logger.error("未找到麦克风设备")
            # [补丁] 作者原本这里是 input('按回车键退出') + sys.exit(1)。
            # 但程序是用隐藏控制台启动的，input() 会**永久挂住**（没有任何
            # 窗口能按回车），从任务管理器看就是"卡死"。改成直接报错退出。
            console.print('[red]未找到麦克风设备，'
                          '请检查系统「隐私和安全性 > 麦克风」权限[/red]')
            sys.exit(1)

        # 创建音频流
        try:
            stream = sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                blocksize=int(self.BLOCK_DURATION * self.SAMPLE_RATE),
                # [补丁] 用上面 resolve_input_device() 解析出的设备；
                # getattr 兜底是为了「解析前就抛异常」的分支下仍能回落到默认
                device=getattr(self, '_device_index', None),
                dtype="float32",
                channels=self._channels,
                callback=self._audio_callback,
                finished_callback=self._on_stream_finished,
            )
            stream.start()

            self.state.stream = stream
            self._running = True
            self._last_active = time.time()   # [补丁] 刚打开，空闲计时从头算
            logger.debug(
                f"音频流已启动: 采样率={self.SAMPLE_RATE}, "
                f"块大小={int(self.BLOCK_DURATION * self.SAMPLE_RATE)}"
            )
            return stream

        except sd.PortAudioError as e:
            logger.error(f"创建音频流失败: {e}", exc_info=True)
            if '-9999' in str(e):
                console.print("""
[bold red]检测到麦克风被占用或权限异常（错误码 -9999）[/bold red]
请尝试以下解决方案：

  1. 设置 > 隐私和安全性 > 麦克风，将「允许桌面应用访问麦克风」打开
  2. 状态栏右下角音量图标 > 右键菜单 > 声音 > 麦克风的属性，关闭「允许应用程序独占控制该设备」
  3. 状态栏右下角音量图标 > 右键菜单 > 声音 > 麦克风的属性，关闭「增强效果」
""")
            return None
        except Exception as e:
            logger.error(f"创建音频流失败: {e}", exc_info=True)
            return None

    def stop(self) -> None:
        """停止音频流"""
        if not self._running:
            return

        self._running = False  # 标记为停止
        if self.state.stream is not None:
            try:
                self.state.stream.close()
                logger.debug("音频流已停止")
            except Exception as e:
                logger.debug(f"停止音频流时发生错误: {e}")
            finally:
                self.state.stream = None

    def reopen(self) -> Optional[sd.InputStream]:
        """
        重新启动音频流

        Returns:
            新创建的音频输入流
        """
        logger.info("正在重启音频流...")

        # 停止旧流
        self.stop()

        # 重载 PortAudio，更新设备列表
        try:
            sd._terminate()
            sd._ffi.dlclose(sd._lib)
            sd._lib = sd._ffi.dlopen(sd._libname)
            sd._initialize()
        except Exception as e:
            logger.warning(f"重载 PortAudio 时发生警告: {e}")

        # 等待设备稳定
        time.sleep(0.1)

        # 启动新流
        return self.start()
