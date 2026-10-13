# coding: utf-8
"""
`--settings` 独立设置模式（逃生通道）

解决什么问题：
    托盘的启用依赖控制台窗口句柄，而且它是**设置面板与「退出」的唯一图形入口**。
    一旦托盘起不来（用 pythonw / 重定向输出 / 某些启动器启动会丢掉控制台），
    用户就再没有任何图形入口能改配置；配置改坏了导致客户端起不来时同理。

    这个模式提供一个**不启动客户端**的设置窗口：不连服务端、不注册全局快捷键、
    不建托盘、不碰音频与模型。只做一件事 —— 把配置写进 config_local.json，
    并告诉用户"下次启动生效"。

与正常运行的关系：
    写盘走的是同一个 core/client/settings_apply.apply_result()（app=None 分支），
    所以字段处理、校验、脱敏、深合并的行为完全一致，不会出现"两种模式写出来的
    配置不一样"。

用法：
    Speak More.exe --settings          （打包版）
    python start_client.py --settings  （源码）
"""

from __future__ import annotations

import sys
from typing import Any, Dict, Optional


def _setup_logging():
    """
    用与客户端相同的那套日志（logs/client_latest.log）—— 排查时不用找第二个地方。

    刻意**不走** `from core.client import logger`：那会顺带 import 整个客户端包
    （音频/快捷键/托盘/LLM…），而这正是"客户端可能起不来"时最不该碰的东西。
    """
    try:
        from core.logger import get_logger, setup_logger
        from config_client import ClientConfig as Config
        setup_logger('client', level=getattr(Config, 'log_level', 'INFO'))
        return get_logger('client')
    except Exception:
        import logging
        logging.basicConfig(level=logging.INFO)
        return logging.getLogger('standalone')


def build_standalone_snapshot(logger) -> Dict[str, Any]:
    """
    拼一份给设置面板用的快照。

    与正常运行的区别只有一个：app 不存在，所以 LLM 角色直接向 RoleLoader 要
    （和 LLMHandler 启动时做的是同一件事），麦克风试录则不可用（没有音频流）。
    """
    from core.ui.settings_handler import build_snapshot

    class _RolesHolder:
        """只为让 build_snapshot 能拿到 app.llm.roles 而存在的最小替身。"""
        def __init__(self, roles):
            class _LLM:
                pass
            self.llm = _LLM()
            self.llm.roles = roles

    roles: Dict[str, Any] = {}
    try:
        from core.client.llm.llm_role_loader import RoleLoader
        loader = RoleLoader()
        loader.load_all_roles()
        roles = loader.get_roles()
        logger.info(f'独立设置模式：已加载角色 {list(roles.keys())}')
    except Exception as e:
        # 角色读不出来不致命：面板里 LLM 那几格会是空的，
        # 但用户不动它们就不会写盘（apply_result 只写真的改过的项）。
        logger.warning(f'独立设置模式：加载 LLM 角色失败（面板里 LLM 一栏会空着）: {e}')

    return build_snapshot(_RolesHolder(roles))


def run_standalone_settings() -> int:
    """跑一次独立设置。返回进程退出码。"""
    logger = _setup_logging()
    logger.info('=' * 60)
    logger.info('独立设置模式（--settings）：不启动客户端，只改配置')
    logger.info('=' * 60)

    try:
        import tkinter as tk
    except Exception as e:
        _fatal(f'无法加载图形界面（tkinter）: {e}')
        return 1

    try:
        snapshot = build_standalone_snapshot(logger)
    except Exception as e:
        _fatal(f'读取当前配置失败: {e}')
        return 1

    root = None
    try:
        root = tk.Tk()
        root.withdraw()                     # 只要一个 Tk 根，不要空窗口

        from core.ui.key_capture import capture_next_key
        from core.ui.settings_dialog import SettingsDialog

        def _capture(timeout: float = 15.0, cancel: Optional[Any] = None):
            # 独立模式没有宿主快捷键监听要暂停，app 传 None 即可
            return capture_next_key(None, timeout=timeout, cancel=cancel)

        dialog = SettingsDialog(root, mic_apply=None, key_capture=_capture)
        result = dialog.show(snapshot)
    except Exception as e:
        logger.error(f'打开设置窗口失败: {e}', exc_info=True)
        _fatal(f'打开设置窗口失败: {e}')
        return 1
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass

    if result is None or not getattr(result, 'confirmed', False):
        logger.info('独立设置模式：用户取消，未做任何改动')
        return 0

    # ---- 写盘（与正常运行同一条路径，app=None 表示不热生效）----
    try:
        from core.client.settings_apply import apply_result, summarize
        report = apply_result(None, result.data, snapshot)
    except Exception as e:
        logger.error(f'保存设置失败: {e}', exc_info=True)
        _fatal(f'保存设置失败: {e}')
        return 1

    text = summarize(report) if report else ''
    logger.info(f'独立设置模式：保存完成 —— {text or "无改动"}')

    if getattr(report, 'errors', None):
        _info('保存时有问题：\n\n' + '\n'.join(report.errors))
        return 1

    if report and (report.applied or report.restart):
        _info('设置已保存。\n\n'
              '因为客户端现在没有运行，改动会在**下次启动**时生效。')
    else:
        _info('没有检测到改动。')
    return 0


def _fatal(message: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, message, 'Speak More 独立设置模式', 0x10)     # MB_ICONERROR
    except Exception:
        print(message, file=sys.stderr)


def _info(message: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, message, 'Speak More 独立设置模式', 0x40)     # MB_ICONINFORMATION
    except Exception:
        print(message)
