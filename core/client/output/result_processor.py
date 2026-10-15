# coding: utf-8
"""
识别结果处理模块

提供 ResultProcessor 类用于处理服务端返回的识别结果。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Optional

from config_client import ClientConfig as Config
from core.client.state import console
from core.protocol import RecognitionMessage

from core.client.output.text_output import TextOutput
from core.tools.window_detector import get_active_window_info
import keyboard
from . import logger

from core.client.udp.udp_broadcaster import broadcast_output_udp
from core.tools.format_tools import has_content
from core.tools.zhconv import convert as zhconv_convert
from core.client.audio.file_manager import AudioFileManager
from core.client.llm.llm_write_md import write_llm_md

if TYPE_CHECKING:
    from core.client.state import ClientState
    from core.client.app import CapsWriterClient
    from core.client.hotword.manager import HotwordManager
    from core.client.diary.diary_writer import DiaryWriter



def _estimate_tokens(text: str) -> int:
    """估算文本的 token 数"""
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)


# [补丁] 识别结果里有没有"实义字符"（汉字 / 字母 / 数字）。
# 纯标点、纯空白一律算"没有内容" —— 见 _handle_message 里那处空结果拦截。
# 判据本体放在 core/tools/format_tools.py：llm_handler 那边（角色前缀被剥光之后的
# 空内容）要用同一套判据，两处不能各写一份。
_has_content = has_content


# 录到这么久却一个字都没识别出来 → 值得提醒一次（多半是麦克风没声音）
_SILENCE_WARN_SECONDS = 1.5
_SILENCE_WARN_GAP = 30.0        # 两次提醒之间至少隔这么久，别刷屏
_last_silence_warn = 0.0


def _should_warn_silence(duration: float) -> bool:
    """这次空结果值不值得弹提示（录得够长，且距上次提示够久）。"""
    global _last_silence_warn
    if (duration or 0) < _SILENCE_WARN_SECONDS:
        return False
    now = time.monotonic()
    if now - _last_silence_warn < _SILENCE_WARN_GAP:
        return False
    _last_silence_warn = now
    return True


def _warn_silence() -> None:
    """
    提示"没听到声音"。

    用作者那套 toast（自己的线程 + 自己的 Tk root），不碰主线程的 Tk。
    弹不出来也绝不影响主流程。
    """
    try:
        from core.ui.toast import toast
        toast('🔇 没听到声音\n说了话但一个词都没识别出来，检查一下麦克风'
              '（是不是静音了、或者输入音量被拉到 0）',
              font_size=11, duration=6000, bg='#B45309', initial_width=0.3)
    except Exception as e:
        logger.debug(f'麦克风提示弹不出来（不影响使用）: {e}')


async def _auto_enter(delay: float) -> None:
    """延迟发送回车键"""
    await asyncio.sleep(delay)
    keyboard.press_and_release('enter')
    logger.debug(f"自动回车已发送 (延迟 {delay}s)")


class ResultProcessor:
    """
    识别结果处理器
    
    负责处理服务端返回的识别结果：
    - 接收 WebSocket 消息
    - 执行热词替换
    - 可选地调用 LLM 进行润色
    - 输出最终文本
    - 保存录音和日记
    """
    
    def __init__(self, app: CapsWriterClient):
        """
        初始化结果处理器

        Args:
            app: 客户端 App 实例
        """
        self.app = app
        self._exit_event = asyncio.Event()
        self._loop = asyncio.get_running_loop()  # 保存事件循环引用

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    @property
    def ws(self) -> WebSocketManager:
        """快捷访问连接管理器"""
        return self.app.ws

    @property
    def hotword(self) -> HotwordManager:
        """快捷访问热词管理器"""
        return self.app.hotword

    @property
    def output(self) -> TextOutput:
        """快捷访问文本输出器"""
        return self.app.output

    @property
    def diary(self) -> DiaryWriter:
        """快捷访问日记写入器"""
        return self.app.diary

    def request_exit(self):
        """请求退出处理循环（线程安全）"""
        logger.info("收到退出请求，设置退出事件")

        # 线程安全地设置事件
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._exit_event.set)
            logger.debug("已通过 call_soon_threadsafe 设置退出事件")
        else:
            self._exit_event.set()
            logger.debug("已直接设置退出事件")
    
    def _format_llm_result(self, llm_result) -> str:
        """格式化 LLM 结果输出"""
        polished_text = llm_result.result
        role_name = llm_result.role_name
        processed = llm_result.processed
        token_count = llm_result.token_count
        generation_time = llm_result.generation_time  # 使用生成时间（从第一个 token 开始）

        polished_text = polished_text.replace('\n', ' ').replace('\r', ' ')
        max_display_length = 50
        if len(polished_text) > max_display_length:
            polished_text = polished_text[:max_display_length] + '...'

        role_label = f'[{role_name}]' if role_name else ''
        result_text = f'[green]{polished_text}[/green]' if processed else polished_text

        if token_count == 0 and polished_text:
            token_count = _estimate_tokens(polished_text)

        # 使用生成时间计算速度（更准确）
        if processed and generation_time > 0:
            speed = token_count / generation_time if token_count > 0 else 0
            speed_label = f'    {speed:.1f} tokens/s' if speed > 0 else ''
        else:
            speed_label = ''

        return f'    模型结果{role_label}：{result_text}{speed_label}'
    
    def _log_modifier_key_state(self) -> None:
        """
        检测并记录当前按下的所有键
        
        用于调试按键卡住问题。
        """
        try:
            import keyboard
            
            # 获取所有当前按下的键
            pressed_keys = keyboard._pressed_events
            
            # if pressed_keys:
            key_names = list(pressed_keys.keys())
            logger.debug(f"当前按下的键: {key_names}")
                
        except Exception as e:
            logger.debug(f"检测按键状态失败: {e}")
    
    async def start(self) -> None:
        """开启工作循环（含自动重联）"""
        while not self._exit_event.is_set():
            # 1. 尝试连接，失败则重试
            if not await self.ws.connect():
                await asyncio.sleep(2)
                continue

            # 2. 消息接收循环
            while not self._exit_event.is_set():
                try:
                    message = await self.ws.receive()
                    if message is None: break
                    await self._handle_message(message)
                except Exception as e:
                    logger.debug(f"连接异常中断: {e}")
                    break

            console.print(f'[bold red]已断开服务端连接[/bold red]\n')
            self._cleanup()
            

    async def _handle_message(self, message: Optional[RecognitionMessage]) -> None:
        """处理接收到的消息"""
        if message is None:
            return


        # 使用 text 字段（简单拼接结果，用于语音输入）
        text = message.text
        original_text = text  # 保存原始识别结果
        delay = message.time_complete - message.time_submit

        if message.is_final:
            logger.info(f"收到最终识别结果: {text}, 时延: {delay:.2f}s")
        else:
            logger.debug(
                f"接收到识别结果，文本: {text[:50]}{'...' if len(text) > 50 else ''}, "
                f"时延: {delay:.2f}s"
            )

        # 如果非最终结果，继续等待
        if not message.is_final:
            return

        # [补丁] 空结果就地丢掉，绝不往下走。
        #
        # 没说话 / 麦克风没声音 / 误触快捷键时，服务端会回一个空文本的最终结果。
        # 作者原版会拿这个空字符串一路走完后面的流程 —— **包括调用 LLM 和上屏**，
        # 于是空输入喂给润色模型，模型只能回一句"你好，请把需要校对的语音转写
        # 文本发给我"，而这句话会被当成识别结果**打进用户正在写的文档里**；
        # 顺带还白花一次 API 调用、写一条空日记、广播一条空 UDP、覆盖剪贴板。
        #
        # 实测踩到过：单击模式下按住快捷键会触发一串空会话（见
        # core/client/shortcut/event_handler.py 的自动重复补丁），用户文档里
        # 被塞了十几遍那句客套话。空结果没有任何可处理的内容，到此为止。
        if not _has_content(text):
            logger.debug('识别结果为空（没说话 / 麦克风没声音 / 误触），已忽略')
            console.print('    [yellow]识别结果为空，已忽略（没说话？或者麦克风没声音）')
            # [补丁] 录了挺久却一个字都没识别出来时要**说出来**。
            #
            # 静默丢弃会让用户以为"按了没反应、软件坏了"，而真实原因往往是
            # 麦克风静音 / 系统把输入音量拉到 0 / 选错了设备（实测踩到：
            # 录了 7.55 秒，音频电平每 0.5 秒都是 -95~-101 dBFS，完全是静音）。
            # 只在这种"明显说了话却什么都没听到"的情况下提示，避免误触刷屏。
            if _should_warn_silence(message.duration):
                console.print('    [yellow]这次录了 '
                              f'{message.duration:.1f} 秒却什么都没识别出来 —— '
                              '检查麦克风是不是静音了')
                _warn_silence()
            self._drop_audio_file(message.task_id)
            return

        # 繁体转换
        if Config.traditional_convert:
            try:
                text = zhconv_convert(text, Config.traditional_locale)
            except Exception as e:
                logger.warning(f"繁体转换失败: {e}")

        # 1. 音素检索，热词替换
        hotword_start = time.monotonic()
        correction_result = self.hotword.get_phoneme_corrector().correct(text, k=10)
        if Config.hot:
            text = correction_result.text

        # 2. 去掉末尾符号
        text = TextOutput.strip_punc(text)

        # 3. 正则替换
        text = self.hotword.get_rule_corrector().substitute(text)
        hotword_elapsed = time.monotonic() - hotword_start

        # [补丁] 后处理之后变成**空串**了也要丢：比如 hot-rule.txt 里有把某个
        # 口头词整句替换成空（"[breath]" → ""）的规则。这时候上屏没有意义，
        # 送到 LLM 那边就是"空输入"，模型照样会回一句客套话打进文档。
        #
        # ⚠️ 这里判的是**空串**，不是"有没有实义字符"。曾经用 _has_content 判，
        # 结果误杀了规则替换的合法产物 —— 仓库自带的 hot-rule.txt 就有
        # `句号 → 。`（还有 逗号/问号/回车），你说"句号"本来能打出"。"，
        # 被那道闸静默丢掉了（回归，实测复现过）。
        # 至于"识别结果直接就是纯标点"（单蹦一个"。"）—— 那是**第一道**闸按
        # 原始文本拦的，这里不必重复管。
        if not text:
            logger.debug('识别结果经过标点清理 / 规则替换后是空的，已忽略')
            self._drop_audio_file(message.task_id)
            return

        # 保存最近一次识别结果
        self.state.last_recognition_text = text

        # 控制台输出：时延 + 热词时延合并到一行
        hotword_label = f'  热词时延: {hotword_elapsed:.2f}s' if Config.hot else ''
        console.print(f'    转录时延：{delay:.2f}s{hotword_label}')

        # 先显示原始识别结果
        original_text_stripped = TextOutput.strip_punc(original_text)
        console.print(f'    识别结果：[green]{original_text_stripped}')

        # 如果发生了热词替换，显示替换后的结果
        if original_text_stripped != text:
            console.print(f'    热词替换：[cyan]{text}')
            logger.debug(f"热词替换后: {text[:50]}{'...' if len(text) > 50 else ''}")

        # 热词匹配情况
        matched_hotwords = correction_result.matches
        potential_hotwords = correction_result.similars

        # 1. 显示完全匹配/已替换的热词
        if matched_hotwords and Config.hot:
            # 提取热词文本 (现为 (原词, 热词, 分数))
            replaced_info = [f"{origin}->[green4]{hw}[/]" for origin, hw, score in matched_hotwords]
            console.print(f'    完全匹配：{", ".join(replaced_info)}')

        # 2. 潜在热词记录到 log
        if potential_hotwords and Config.hot:
            replaced_set = {hw for origin, hw, score in matched_hotwords}
            potential_matches = [(origin, hw, score) for origin, hw, score in potential_hotwords if hw not in replaced_set]
            if potential_matches:
                log_str = "; ".join([f"{origin}->{hw}({score:.2f})" for origin, hw, score in potential_matches])
                logger.debug(f"潜在热词: {log_str}")

        # 窗口兼容性检测
        paste = Config.paste
        process_name = get_active_window_info().get('process_name', '')
        logger.debug(f"当前活动窗口: {process_name}")
        if any(app.lower() == process_name.lower() for app in Config.paste_apps):
            paste = True
            logger.debug(f"检测到兼容性应用: {process_name}，使用粘贴模式")

        # 自动回车检测
        for app, delay in Config.enter_apps:
            if app.lower() == process_name:
                asyncio.create_task(_auto_enter(delay))

        # LLM 处理和输出
        llm_result = None
        if Config.llm_enabled:
            llm_result = await self.app.llm.process_and_output(
                text,
                paste=paste,
                matched_hotwords=potential_hotwords  # 传递上下文热词给 LLM
            )
        else:
            await self.output.output(text, paste=paste)
            self.state.set_output_text(text)
            broadcast_output_udp(text)

        # [补丁] **这一次**真正上屏的文本，后面剪贴板和「从纠正里学词」都用它。
        #
        # ⚠️ 千万别写成 `self.state.last_output_text or text` —— 那个字段是
        # "最近一次**成功**上屏的文本"，LLM 失败时（认证错 / 超时 / 粘贴模式下按
        # ESC 中断 / toast 建不出来）它**不会被刷新**，于是这里会拿到**上一次
        # 说的那句话**：剪贴板被写成上一句，「学词」也会拿上一句去回读比对。
        # 实测复现过三种失败场景（logs/verify_stale_last_output_text.py）。
        #
        # 本次自己的结果优先；LLM 那条路返回空（失败）时，屏幕上留的是原文
        # （handle_llm_error 的 should_fallback_to_original 会补打一次原文），
        # 所以退回 text 是对的。
        final_text = text
        if llm_result is not None and getattr(llm_result, 'result', ''):
            final_text = llm_result.result

        # [补丁] 把结果自动放一份到剪贴板。
        #
        # 上屏是直接打到光标处的，但用户常常还想把它粘到别的地方；
        # 而且万一某个程序不吃模拟键盘输入，剪贴板还能手动 Ctrl+V 兜底。
        # 托盘菜单的「📋 复制结果」本来就是干这个的，只是要手动点一下 ——
        # 这里改成每次都自动放一份。
        #
        # 位置放在两条分支合流之后是有讲究的：
        #   - 粘贴模式下 output() 的流程是"写剪贴板 → Ctrl+V → 还原旧内容"，
        #     我们在这之后覆盖，剪贴板里留下的正好是识别结果；
        #   - LLM 分支是 await 完的，此时 final_text 已经是润色后的最终文本。
        if getattr(Config, 'copy_result_to_clipboard', True):
            try:
                from ..llm.llm_clipboard import copy_to_clipboard
                if final_text:
                    copy_to_clipboard(final_text)
                    logger.debug(f'识别结果已复制到剪贴板（{len(final_text)} 字）')
            except Exception as e:
                logger.debug(f'复制结果到剪贴板失败（不影响上屏）: {e}')

        # [补丁] 从你的纠正里学词（对标 Typeless 的 Auto-added）。
        #
        # 记下刚上屏的这段文本，几秒后回读同一个输入框：如果里面还是这句话、
        # 只是有几个词被改过，就把改动收进词典。实现见 correction_learner.py，
        # 它起个后台线程就返回，**不给上屏这条主链路加延迟**。
        #
        # 传 final_text（润色后的、真正在屏幕上的那句），不是原始识别文本 ——
        # 要比对的是屏幕上那一份。开关在设置面板「热词」页。
        if getattr(Config, 'learn_from_corrections', True):
            try:
                from ..correction_learner import watch as _watch_corrections
                _watch_corrections(final_text)
            except Exception as e:
                logger.debug(f'学词监视启动失败（不影响上屏）: {e}')

        # 保存录音与写入 md 文件
        file_audio = None
        if Config.save_audio:
            # 重命名音频文件
            file_path = self.state.pop_audio_file(message.task_id)
            if file_path:
                file_manager = AudioFileManager()
                file_manager.file_path = file_path
                file_audio = file_manager.rename(text, message.time_start)

            # 写入日记
            self.diary.write(text, message.time_start, file_audio)

        # LLM 结果显示和保存
        if Config.llm_enabled and llm_result and llm_result.processed:
            console.print(self._format_llm_result(llm_result))
            write_llm_md(
                llm_result.input_text,
                llm_result.result,
                llm_result.role_name,
                message.time_start,
                file_audio
            )

        # 检测修饰键状态（调试用）
        self._log_modifier_key_state()

        console.line()

    def _drop_audio_file(self, task_id: str) -> None:
        """
        空结果早退时的收尾：把这次会话登记的录音**摘掉登记**。

        [补丁] 为什么必须做：录音文件是在录音器里 create + register_audio_file
        登记的，正常路径末尾（save_audio 那段）才会 pop 出来改名、挂进日记。
        空结果提前 return 就永远走不到那里 —— `ClientState.audio_files` 里的
        登记项要到程序退出才清（reset() 只在 app.stop() 时调），而磁盘上会留下
        一个**带随机后缀、日记里没链接、记录页也看不到**的孤儿 mp3
        （实测漏过：麦克风静音时每按一次就多一个）。

        **不删文件**："识别结果为空"不等于"这段录音没用"（模型出问题时结果也是空的），
        真要清理就在 assets/ 目录里按需删。
        """
        try:
            path = self.state.pop_audio_file(task_id)
            if path:
                logger.debug(f'空结果：已摘掉这次录音的登记（文件保留）: {path}')
        except Exception as e:
            logger.debug(f'摘录音登记失败（不影响流程）: {e}')

    def _cleanup(self) -> None:
        """清理资源"""
        if self.state.websocket is not None:
            try:
                if self.state.websocket.closed:
                    self.state.websocket = None
                    logger.debug("WebSocket 连接已清理")
            except Exception:
                self.state.websocket = None
