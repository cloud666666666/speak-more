# coding: utf-8
"""
服务端模块

提供 CapsWriter 服务端的所有功能模块。

模块架构：
- context: 全局上下文管理（连接池、消息队列）
- schema: 数据类定义（Task, Result）
- check_model: 模型文件检查
- worker: 识别工作进程入口 (由 init_recognizer 处理)
- recognize: 语音识别核心流程
- ws_recv: WebSocket 接收处理
- ws_send: WebSocket 发送处理
"""

from core.server.state import console
from core.logger import get_logger, setup_logger
from config_server import ServerConfig as Config, __version__

setup_logger('server', level=Config.log_level)
logger = get_logger('server')

# [补丁] 补打显卡探测结论。
# 探测发生在上面那句 import config_server 的模块体里，那时 logger 还没建好，
# 打日志会丢，所以只能在这里补一行（探测结果由 gpu_probe 暂存）。
try:
    from core.tools import gpu_probe as _gpu_probe
    _gpu_decision = _gpu_probe.last_decision()
    if _gpu_decision:
        logger.info(_gpu_probe.summary_line(_gpu_decision))
except Exception:
    pass

from core.server.schema import Task, Result

__all__ = [
    'console',
    'logger',
    'Task',
    'Result',
]
