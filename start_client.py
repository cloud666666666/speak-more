# coding: utf-8
import os
import sys

if __name__ == "__main__":
    # [补丁] --settings：独立设置模式（逃生通道）。
    # 托盘起不来（没有控制台窗口）或配置改坏导致客户端起不来时，用它改配置。
    # 这个分支刻意放在 import 客户端之前 —— 它不碰客户端那套依赖
    # （音频 / 快捷键 / 托盘 / LLM / 服务端），只开一个设置窗口写配置文件。
    if '--settings' in sys.argv[1:]:
        from core.client.standalone_settings import run_standalone_settings
        sys.exit(run_standalone_settings())

    # 直接实例化并启动门面类即可
    # 环境初始化职责已下放至 CapsWriterClient
    from core.client import CapsWriterClient
    CapsWriterClient().start()
