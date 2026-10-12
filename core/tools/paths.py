# coding: utf-8
"""
路径工具：定位「程序根目录」。

为什么不能到处写 `Path(__file__).resolve().parents[N]`：

打包时 `core/` 可能是**目录联接**（本仓库 build.spec 就是用 `mklink /j` 把它
联进发行目录的），而 `Path.resolve()` 会把联接展开成源码树的**真实路径** ——
于是打包版算出来的"安装目录"指向源码树，跟进程实际的工作目录对不上。

实测到的后果（都是打包版才会暴露）：
  - 孤儿清理的 cwd 判据永远不匹配 → 清理静默失效
  - 设置面板的「打开日志文件夹 / 打开安装目录」打开的是源码树
  - 「热词」页编辑的是源码树里的 hot.txt
  - 显卡探测的缓存与引擎 bin 扫描指向源码树

判据用 `sys.frozen`：冻结形态下安装目录就是 exe 所在目录，这跟
`core/client/server_lifecycle._server_command()`、`config_local.py` 的
冻结分支是同一套口径。
"""

from __future__ import annotations

import sys
from pathlib import Path


def install_root() -> Path:
    """程序根目录：发行版里 = exe 同级目录；源码运行时 = 仓库根目录。"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]
