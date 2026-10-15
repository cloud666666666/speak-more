"""
LLM Typing 输出模式

直接打字输出，根据 paste 参数或 Config.paste 选择：
- paste=True: 等流式输出完成后一次性粘贴
- paste=False: 实时流式 write，每个字都打出来
"""
import asyncio
import keyboard

from config_client import ClientConfig as Config
from core.tools.asyncio_to_thread import to_thread
from core.client.output.text_output import TextOutput
from core.client.clipboard import paste_text
from . import logger


async def handle_typing_mode(handler, text: str, paste: bool = None, matched_hotwords=None, role_config=None, content=None) -> tuple:
    """打字输出模式"""
    from .llm_error_handler import handle_llm_error
    # 如果没传，则现场检测一次（兼容性）
    if not role_config or content is None:
        role_config, content = handler.detect_role(text)
    
    if not role_config:
        # 不应发生，但作为防守
        result_text = TextOutput.strip_punc(text)
        await output_text(result_text, paste)
        return (result_text, 0, 0.0)

    handler.monitor.reset()  # 重置停止标志

    try:
        if paste:
            return await _process_paste(handler, role_config, content, matched_hotwords)
        else:
            return await _process_streaming(handler, role_config, content, matched_hotwords)

    except Exception as e:
        result_text, _ = handle_llm_error(e, content, role_config.name if role_config else "LLM")
        result_text = TextOutput.strip_punc(result_text)
        await output_text(result_text, paste)
        return (result_text, 0, 0.0)


async def _process_paste(handler, role_config, content, matched_hotwords) -> tuple:
    """处理粘贴模式：获取全文后一次性粘贴"""
    polished_text, token_count, gen_time = await to_thread(
        handler.process, role_config, content, matched_hotwords, None
    )
    if handler.monitor.should_stop():
        return ("", 0, 0.0)

    final_text = TextOutput.strip_punc(polished_text or content)
    await paste_text(final_text, restore_clipboard=Config.restore_clip)
    return (final_text, token_count, gen_time)


async def _process_streaming(handler, role_config, content, matched_hotwords) -> tuple:
    """处理流式打字模式：边生成边模拟按键打字"""
    chunks = []
    written = []                 # 真正打进文档的那些片段（[补丁] 中途失败时要用）
    pending_buffer = ""

    def stream_write_chunk(chunk: str):
        nonlocal pending_buffer
        if not chunk: return
        chunks.append(chunk)

        full_current = pending_buffer + chunk
        content_to_write = full_current
        trailing = ""

        # 从右向左寻找第一个非 trash 字符
        for i in range(len(full_current) - 1, -1, -1):
            char = full_current[i]
            if char == '\n' or char in Config.trash_punc:
                continue
            else:
                content_to_write = full_current[:i+1]
                trailing = full_current[i+1:]
                break
        else:
            content_to_write = ""
            trailing = full_current

        if content_to_write:
            logger.debug(f"output_text: keyboard.write '{content_to_write}'")
            keyboard.write(content_to_write)
            written.append(content_to_write)
            pending_buffer = trailing
        else:
            pending_buffer = trailing

    # 执行流式处理
    #
    # [补丁] 中途失败时**不能再往外抛异常**。
    #
    # 外面那层 except（handle_typing_mode:39-43）会把**整句原文**补打一遍，
    # 可这里早就把前半句润色文打进文档了 —— 两者在光标处拼起来就是
    # "半截润色文 + 整句原文"，用户文档里凭空多出一段重复内容
    # （实测复现：logs/verify_stream_fallback.py；read 超时只有 2 秒，
    # deepseek 这类接口一卡就会撞上）。
    # 所以：只有"一个字都还没打出去"时才往外抛，让外层照旧降级打原文；
    # 已经打出去半截的，就地收尾（和用户按 ESC 停下来的处理一致）。
    try:
        polished_text, token_count, gen_time = await to_thread(
            handler.process, role_config, content, matched_hotwords, stream_write_chunk
        )
    except Exception as e:
        if not written:
            raise
        final_text = TextOutput.strip_punc(''.join(written))
        logger.warning(f'流式输出中途失败，已经把 {len(final_text)} 字打进文档，'
                       f'就地收尾、不再补打原文（否则会重复）: {e!r}')
        # 半句话留在文档里得说一声，不然用户不知道是被截断了
        try:
            from .llm_error_handler import show_error_notification
            show_error_notification(e, f'{role_config.display_name or "LLM"}'
                                       f'（输出中断，已保留前半句）')
        except Exception:
            pass
        return (final_text, 0, 0.0)

    # 阻塞，直到正常结束，或用户按下 ESC
    if handler.monitor.should_stop():
        final_text = TextOutput.strip_punc(''.join(chunks) or content)
        return (final_text, 0, 0.0)

    # 如果模型没有任何输出，直接打出原文字
    if not chunks:
        final_text = TextOutput.strip_punc(content)
        logger.debug(f"output_text: keyboard.write '{final_text}' (降级)")
        keyboard.write(final_text)
        return (final_text, 0, 0.0)
    
    # 如果 LLM 只输出标点，会被拦截，就要做补偿输出
    full_output = ''.join(chunks).strip()
    if len(full_output) == 1 and full_output in Config.trash_punc:
        keyboard.write(full_output)
    
    return (TextOutput.strip_punc(polished_text), token_count, gen_time)


async def output_text(text: str, paste: bool = None):
    """输出文本（根据 paste 或 Config.paste 选择方式）"""
    if paste:
        await paste_text(text, restore_clipboard=Config.restore_clip)
    else:
        logger.debug(f"output_text: keyboard.write '{text}'")
        keyboard.write(text)
