# coding: utf-8
"""
模型检查模块

检查配置的语音模型文件是否存在，如果不存在则提供下载链接。
"""

import sys
from pathlib import Path

from config_server import ServerConfig as Config
from config_server import ModelPaths, ModelDownloadLinks
from core.server.state import console
from core.tools.model_paths import find_missing_models
from . import logger


def _exit_after_prompt() -> None:
    """
    退出前等人按回车。

    [补丁] 只在**真的有交互终端**时才等：服务端通常是被客户端用
    stdin=DEVNULL + CREATE_NO_WINDOW 拉起来的，那种情况下 input() 会直接抛
    EOFError —— 用户看到的是任务管理器里"服务端一闪就没"，日志里还多一段
    看不懂的 traceback。现在直接干净退出，退出码一样是 1。
    """
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input('按回车退出')
    except Exception:
        pass
    sys.exit(1)


def check_model() -> None:
    """
    根据配置的模型类型检查所需的模型文件是否存在

    如果模型文件不存在，显示错误信息和下载链接后退出程序。

    判断逻辑复用 core.tools.model_paths.find_missing_models() —— 与客户端的
    启动预检是**同一份判断**，免得两边结论不一致（客户端说没问题、服务端说缺）。

    Raises:
        SystemExit: 当模型类型不支持或模型文件缺失时退出
    """
    info = find_missing_models(Config.model_type)
    model_type = info['model_type']
    logger.debug(f"检查模型文件, 类型: {model_type}")

    if not info['supported']:
        error_msg = f"不支持的模型类型: {Config.model_type}"
        logger.error(error_msg)
        console.print(f'''
    [bold red]不支持的模型类型：{Config.model_type}[/bold red]

    请在 config_server.py 中将 ServerConfig.model_type 设置为：
    - 'fun_asr_nano'
    - 'sensevoice'
    - 'paraformer'
    - 'qwen_asr'

        ''', style='bright_red')
        _exit_after_prompt()

    missing_files = info['missing']
    if missing_files:
        model_dir = info['model_dir']
        logger.error(f"模型文件检查失败，共 {len(missing_files)} 个文件缺失")
        error_msg = f'\n[bold red]未能找到模型文件[/bold red]\n\n'
        error_msg += f'当前配置的模型类型：[bold yellow]{model_type}[/bold yellow]\n\n'
        for file_path in missing_files:
            error_msg += f'未找到：[bold yellow]{file_path.name}[/bold yellow]\n'

        if info['misplaced']:
            error_msg += f'\n模型文件似乎错放到了上级目录，请确保放到：[bold yellow]{model_dir}[/bold yellow]\n'

        error_msg += f'\n模型发布页面：\n'
        error_msg += f'[cyan]{ModelDownloadLinks.models_page}[/cyan]\n\n'
        error_msg += f'请根据发布页说明，将模型解压到 [cyan]{ModelPaths.model_dir}[/cyan] 下的正确目录\n'
        error_msg += '\n'

        logger.error(error_msg)
        _exit_after_prompt()

    # 所有必需文件检查通过
    logger.info(f"模型文件检查通过 ({model_type})")
    console.print(f'[green4]模型文件检查通过 ({model_type})', end='\n\n')
