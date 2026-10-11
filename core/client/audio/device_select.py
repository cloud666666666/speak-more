# coding: utf-8
"""
麦克风设备枚举与解析

背景：作者的 `core/client/audio/stream.py` 原本**完全没有设备选择能力** ——
`stream.py:115` 硬编码 `sd.query_devices(kind='input')`（系统默认），
`stream.py:135` 硬编码 `device=None`。所以"选麦克风"这个功能必须动那两处。

配置里存的是**设备名**而不是索引：Windows 上插拔一次 USB 麦克风，
设备索引就会整体漂移，按索引存会指向错误的设备。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

try:
    from .. import logger                      # core.client.logger
except Exception:                              # pragma: no cover
    import logging
    logger = logging.getLogger('client')


def list_input_devices() -> List[Tuple[int, str, int]]:
    """
    枚举所有输入设备。

    Returns:
        [(设备索引, 设备名, 最大输入声道数), ...]

    逐设备 try/except：**设备名不是合法 UTF-8 是真实存在的问题**
    （作者在 stream.py:123 已经专门防过 UnicodeDecodeError），
    一个坏设备不能让整个列表枚举失败。
    """
    try:
        import sounddevice as sd
    except Exception as e:
        logger.warning(f'无法导入 sounddevice，麦克风列表为空: {e}')
        return []

    try:
        raw = sd.query_devices()
    except Exception as e:
        logger.warning(f'枚举音频设备失败: {e}')
        return []

    devices: List[Tuple[int, str, int]] = []
    for index, dev in enumerate(raw):
        try:
            if not isinstance(dev, dict):
                continue
            in_ch = int(dev.get('max_input_channels', 0) or 0)
            if in_ch <= 0:                      # 纯输出设备，跳过
                continue
            name = str(dev.get('name', '')).strip() or f'设备 {index}'
            devices.append((index, name, in_ch))
        except UnicodeDecodeError:
            logger.warning(f'设备 {index} 的名称编码有问题，已跳过')
        except Exception as e:
            logger.debug(f'读取设备 {index} 失败，已跳过: {e}')

    return devices


def _default_input_device() -> Tuple[None, Dict[str, Any]]:
    """
    取系统默认输入设备。

    刻意**不吞异常** —— stream.py 依赖 PortAudioError 来打印
    "未找到麦克风设备"并退出，吞掉会让那条路径失效。
    """
    import sounddevice as sd
    return None, sd.query_devices(kind='input')


def resolve_input_device() -> Tuple[Optional[int], Dict[str, Any]]:
    """
    按 config_client.ClientConfig 里的 mic_device / mic_device_index 解析设备。

    匹配顺序：设备名精确 -> 设备名唯一子串 -> 配置的索引 -> 系统默认。

    Returns:
        (device_index 或 None, 设备信息 dict)
        索引为 None 表示用系统默认（等价于改补丁之前的行为）。

    配置为空时直接走 `_default_input_device()`，行为和作者原版完全一致。
    """
    index: Optional[int] = None
    device: Optional[Dict[str, Any]] = None

    wanted_name = ''
    wanted_index = -1
    try:
        from config_client import ClientConfig as Config
        wanted_name = str(getattr(Config, 'mic_device', '') or '').strip()
        wanted_index = int(getattr(Config, 'mic_device_index', -1) or -1)
    except Exception:
        pass

    if not wanted_name and wanted_index < 0:
        return _default_input_device()

    devices = list_input_devices()

    # 1) 名字精确匹配
    if wanted_name:
        match = next((d for d in devices if d[1] == wanted_name), None)

        # 2) 唯一子串匹配（用户改名/驱动升级后名字微变的兜底）
        if match is None:
            partial = [d for d in devices if wanted_name in d[1]]
            if len(partial) == 1:
                match = partial[0]
            elif len(partial) > 1:
                logger.warning(
                    f'麦克风名字 "{wanted_name}" 匹配到多个设备 '
                    f'{[d[1] for d in partial]}，无法确定，改用系统默认')

        if match is not None:
            index, name = match[0], match[1]

    # 3) 按索引兜底
    if index is None and wanted_index >= 0:
        if any(d[0] == wanted_index for d in devices):
            index = wanted_index

    if index is None:
        if wanted_name:
            logger.warning(
                f'配置的麦克风 "{wanted_name}" 不在当前设备列表里，改用系统默认。'
                f'当前可用: {[d[1] for d in devices]}')
        return _default_input_device()

    # 取该设备的详细信息，返回结构和 sd.query_devices(kind='input') 一致
    try:
        import sounddevice as sd
        device = dict(sd.query_devices(index))
    except Exception as e:
        logger.warning(f'读取设备 {index} 详情失败，改用系统默认: {e}')
        return _default_input_device()

    logger.info(f'使用配置的麦克风设备: [{index}] {device.get("name", "?")}')
    return index, device


def describe_current() -> str:
    """给设置面板底部用的一行说明。"""
    try:
        index, device = resolve_input_device()
    except Exception as e:
        return f'（无法解析麦克风：{e}）'
    name = device.get('name', '未知设备') if isinstance(device, dict) else '未知设备'
    if index is None:
        return f'当前使用系统默认设备：{name}'
    return f'当前使用设备 [{index}]：{name}'
