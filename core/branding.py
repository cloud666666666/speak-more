# coding: utf-8
"""
应用对外显示的名字

这是**本分发版自己的品牌名**，和上游项目 CapsWriter-Offline 区分开：
代码里凡是用户能看到的地方（托盘标题、设置窗口标题、提示文案、
启动脚本名）都用这里的常量，不要各写各的。上游的 readme / 版权声明不动。
"""

from __future__ import annotations

APP_NAME = 'Speak More'
APP_NAME_SHORT = 'Speak More'

# 设置面板等窗口标题
SETTINGS_TITLE = f'{APP_NAME} 设置'

# 托盘图标悬浮提示 / 菜单首行
TRAY_TITLE = APP_NAME

# 上游项目信息（保留出处，MIT 协议要求）
UPSTREAM_NAME = 'CapsWriter-Offline'
UPSTREAM_URL = 'https://github.com/HaujetZhao/CapsWriter-Offline'
