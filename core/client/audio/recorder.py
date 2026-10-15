# coding: utf-8
"""
音频录制模块

提供 AudioRecorder 类用于管理录音会话，包括开始录音、
发送音频数据到服务端、结束录音等功能。
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from typing import TYPE_CHECKING, Optional

import numpy as np
import websockets

from config_client import ClientConfig as Config
from core.client.state import console
from core.client.audio.file_manager import AudioFileManager
from core.client.connection import WebSocketManager
from core.protocol import AudioMessage
from . import logger

if TYPE_CHECKING:
    from core.client.state import ClientState
    from core.client.app import CapsWriterClient

# 日志记录器


class AudioRecorder:
    """
    音频录制器
    
    管理一次完整的录音会话，包括：
    - 从音频流接收数据
    - 可选地保存到本地文件
    - 将音频数据发送到识别服务端
    """
    
    def __init__(self, app: CapsWriterClient):
        """
        初始化录制器
        
        Args:
            app: 客户端 App 实例
        """
        self.app = app
        self.task_id: Optional[str] = None
        self._file_manager: Optional[AudioFileManager] = None
        self._start_time: float = 0.0
        self._duration: float = 0.0
        self._cache: list = []

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    @property
    def _ws_manager(self) -> WebSocketManager:
        """快捷访问桥接到 app.ws"""
        return self.app.ws
    
    def _drop_stale_blocks(self, before_time: float) -> int:
        """
        [补丁] 丢掉队列里时间戳早于 `before_time` 的音频块，返回丢了几块。

        用途：本会话读到 finish 就退出循环了，但音频回调有可能在 finish 之后
        又推了一块进来（松开快捷键那一瞬间的竞态）。那块音频留在**所有录音
        会话共用的** queue_in 里，会被下一次录音当成自己的开头 —— 表现为
        下一句开头混进上一句最后的残音。

        实现细节（为什么可以直接动内部 deque）：
          - 本函数跑在事件循环线程上，**中间没有 await**，所以这段操作是原子的；
          - 唯一的两个入队方（音频回调 stream.py、快捷键 task.py）都是通过
            `asyncio.run_coroutine_threadsafe` 把 put 排到同一个循环上的，
            不存在"另一个线程正在 put"的情况；
          - 原地过滤（而不是取出来再放回）不会打乱任何条目的顺序 ——
            取出来再放回会把队头的东西挪到队尾，那才是真会出事。
        """
        queue = self.state.queue_in
        items = getattr(queue, '_queue', None)
        if items is None:                       # 队列实现变了就安静放弃
            return 0
        keep = [it for it in items
                if not (isinstance(it, dict)
                        and it.get('type') == 'data'
                        and it.get('time', 0) <= before_time)]
        dropped = len(items) - len(keep)
        if not dropped:
            return 0
        items.clear()
        items.extend(keep)
        # 丢掉的块也要 task_done 记账，免得 queue.join() 永远等下去
        for _ in range(dropped):
            try:
                queue.task_done()
            except Exception:
                break
        logger.debug(f"清掉 {dropped} 块上一个会话残留的音频")
        return dropped

    async def _send_message(self, message: AudioMessage) -> None:
        """发送消息到服务端"""
        if not self._ws_manager.is_connected:
            if message.is_final:
                self.state.pop_audio_file(message.task_id)
                console.print('    服务端未连接，无法发送\n')
                logger.warning("服务端未连接，无法发送音频数据")
            return
        
        # 使用 WebSocketManager 发送协议消息
        success = await self._ws_manager.send(message)
        if not success and message.is_final:
            self.state.pop_audio_file(message.task_id)
            # 具体错误日志由 WebSocketManager 记录
    
    async def record_and_send(self) -> None:
        """
        录音并发送数据

        从队列中读取音频数据，保存到文件（如果启用），
        并发送到服务端进行识别。
        """
        try:
            # 生成唯一任务 ID
            self.task_id = str(uuid.uuid1())
            logger.debug(f"创建录音任务，任务ID: {self.task_id}")

            self._start_time = 0.0
            self._duration = 0.0
            self._cache = []
            
            # 音频文件管理
            file_path = None
            if Config.save_audio:
                self._file_manager = AudioFileManager()
            
            # 从队列读取数据
            while task := await self.state.queue_in.get():
                self.state.queue_in.task_done()
                
                if task['type'] == 'begin':
                    self._start_time = task['time']
                    logger.debug(f"录音开始，时间戳: {self._start_time}")
                    
                elif task['type'] == 'data':
                    # [补丁] begin 还没轮到就被数据超车时兜个底。
                    #
                    # queue_in 是所有录音会话共用的，会串线（上一个会话的残留、
                    # 或者 begin 与音频数据在线程/事件循环上的排程顺序反过来）。
                    # 那样本会话可能先收到 data，而 _start_time 还是初始的 0.0 ——
                    # 结果这一整段录音的时间戳就是 0：音频存成
                    # (19700101-080000).mp3，日记写进 1970/01/ 里。
                    # 用第一块数据的时间当开始时间，误差几毫秒，总比时间戳归零好。
                    if not self._start_time:
                        self._start_time = task['time']
                        logger.debug(f"未收到 begin，用第一块音频的时间作为开始: "
                                     f"{self._start_time}")

                    # 在阈值之前积攒音频数据
                    if task['time'] - self._start_time < Config.threshold:
                        self._cache.append(task['data'])
                        continue
                    
                    # 创建音频文件
                    if Config.save_audio and self._file_manager and file_path is None:
                        file_path, _ = self._file_manager.create(
                            task['data'].shape[1],
                            self._start_time
                        )
                        self.state.register_audio_file(self.task_id, file_path)
                        logger.debug(f"创建音频文件: {file_path}")
                    
                    # 获取音频数据
                    if self._cache:
                        data = np.concatenate(self._cache)
                        self._cache.clear()
                    else:
                        data = task['data']
                    
                    # 保存音频至本地文件
                    self._duration += len(data) / 48000
                    if Config.save_audio and self._file_manager:
                        self._file_manager.write(data)
                    
                    # 发送音频数据用于识别
                    message = AudioMessage(
                        task_id=self.task_id,
                        source='mic',
                        data=base64.b64encode(
                            np.mean(data[::3], axis=1).tobytes()
                        ).decode('utf-8'),
                        is_final=False,
                        time_start=self._start_time,
                        seg_duration=Config.mic_seg_duration,
                        seg_overlap=Config.mic_seg_overlap,
                        context=Config.context,
                        language=Config.language,
                    )
                    asyncio.create_task(self._send_message(message))
                    
                elif task['type'] == 'finish':
                    # 本次会话的结束时刻（[补丁]：收尾时按它判断哪些音频块是残渣）
                    finish_time = task.get('time') or time.time()
                    # 如果有缓存的数据未发送，先发送缓存
                    if self._cache:
                        data = np.concatenate(self._cache)
                        self._cache.clear()
                        
                        self._duration += len(data) / 48000
                        if Config.save_audio and self._file_manager:
                            self._file_manager.write(data)

                        message = AudioMessage(
                            task_id=self.task_id,
                            source='mic',
                            data=base64.b64encode(
                                np.mean(data[::3], axis=1).tobytes()
                            ).decode('utf-8'),
                            is_final=False,
                            time_start=self._start_time,
                            seg_duration=Config.mic_seg_duration,
                            seg_overlap=Config.mic_seg_overlap,
                            context=Config.context,
                            language=Config.language,
                        )
                        asyncio.create_task(self._send_message(message))

                    # 完成写入本地文件
                    if Config.save_audio and self._file_manager:
                        self._file_manager.finish()
                        logger.debug("完成音频文件写入")
                    
                    console.print(f'任务标识：{self.task_id}')
                    console.print(f'    录音时长：{self._duration:.2f}s')
                    logger.info(f"录音任务完成，任务ID: {self.task_id}, 时长: {self._duration:.2f}s")
                    
                    # 告诉服务端音频片段结束了
                    message = AudioMessage(
                        task_id=self.task_id,
                        source='mic',
                        data='',
                        is_final=True,
                        time_start=self._start_time,
                        seg_duration=Config.mic_seg_duration,
                        seg_overlap=Config.mic_seg_overlap,
                        context=Config.context,
                        language=Config.language,
                    )
                    asyncio.create_task(self._send_message(message))

                    # [补丁] 收尾：把队列里**属于本次会话之前**的残留音频丢掉。
                    #
                    # 松开快捷键的一瞬间，音频回调可能正好通过 state.recording
                    # 检查、把最后一块推了进来（它排在 finish 后面）。本会话读到
                    # finish 就 break 了，那块音频留在**所有会话共用的** queue_in
                    # 里，下一次录音会把它当成自己的第一块发出去 —— 下一句的开头
                    # 就会混进上一句最后 ~50ms 的残音。
                    # 只丢"时间戳早于本次结束时刻"的块，正在路上的新会话音频
                    # 一根汗毛都不动。
                    self._drop_stale_blocks(finish_time)
                    break

        except asyncio.CancelledError:
            # 录音被取消（短按时间过短 / 单击模式超时取消）。
            # 此时本会话的 begin 以及阈值前积攒的 data 可能还残留在全局
            # queue_in 中，而该队列由所有录音会话共用。若不清理，下一次
            # record_and_send() 会读到带旧 task_id 语义的脏数据，导致
            # 「前文丢失 / 旧结果混入」的串线问题。
            drained = 0
            while True:
                try:
                    self.state.queue_in.get_nowait()
                    self.state.queue_in.task_done()
                    drained += 1
                except asyncio.QueueEmpty:
                    break
            logger.debug(
                f"录音任务被取消，已排空队列残留 {drained} 条，任务ID: {self.task_id}"
            )
            raise

        except Exception as e:
            logger.error(f"录音任务错误: {e}", exc_info=True)
    
    def get_file_manager(self) -> Optional[AudioFileManager]:
        """获取当前的文件管理器"""
        return self._file_manager
