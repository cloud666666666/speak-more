# coding: utf-8
"""
开机自启的安装 / 卸载 / 查询

用 `%APPDATA%\\...\\Startup\\Speak More.lnk` 指向 `wscript.exe` 去跑项目里的
`启动 Speak More.vbs`，而不是往 Startup 目录里扔一个 .vbs。**唯一的区别是图标**：

  - `.vbs` 在资源管理器和任务管理器的「启动」页里显示的是 **wscript 的卷轴图标**
  - `.lnk` 可以带 `IconLocation`，指向自己的 `assets/icon.ico`

## 为什么不写 HKCU\\...\\Run

两种做法都是免提权的，但 **任务管理器的「启动」页只列 Startup 文件夹和注册表
Run 两项**，行为一致；而 Startup 文件夹里的东西用户**看得见、能自己删**，
出问题时比注册表好排查。这一版就先用文件夹。

## 踩过的坑

  - **不要设 `WindowStyle`**。给快捷方式设成最小化（7）之后，双击/开机时
    `wscript.exe` 会被以最小化方式拉起，脚本根本跑不起来 ——
    表现是"开机不自启，但手动双击项目里的 .vbs 是好的"，很难查。
    `Description` 一起省了，只留必需的几项。
  - **`wscript.exe` 要写全路径**（`C:\\Windows\\System32\\wscript.exe`）。
    相对名依赖 PATH 解析，开机时机器的环境未必一致。
  - 参数里的路径**可以有中文和空格**，但必须带引号。实测中文路径在 .lnk 里
    没有编码问题（不像 .vbs 内容那样必须 UTF-16）。

## 和旧版的兼容

这一版之前，Startup 里放的是一个 `Speak More.vbs`。`enable()` 会顺手把它删掉，
免得两份都在、开机起两次（虽然 `启动 Speak More.vbs` 是幂等的，但看着脏）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

APP_NAME = 'Speak More'

# .lnk 的文件名（不带扩展名）
LINK_NAME = 'Speak More'
# 项目里真正的启动器
LAUNCHER_VBS = '启动 Speak More.vbs'
# 旧版遗留的 Startup 条目，enable() 时清掉
LEGACY_VBS = 'Speak More.vbs'

WSCRIPT = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'wscript.exe'


def project_root() -> Path:
    """项目根目录（本文件所在目录）。"""
    return Path(__file__).resolve().parent


def startup_dir() -> Path:
    """当前用户的「启动」文件夹。"""
    appdata = os.environ.get('APPDATA') or str(Path.home() / 'AppData' / 'Roaming')
    return (Path(appdata) / 'Microsoft' / 'Windows' / 'Start Menu'
            / 'Programs' / 'Startup')


def link_path() -> Path:
    return startup_dir() / f'{LINK_NAME}.lnk'


def launcher_path() -> Path:
    return project_root() / LAUNCHER_VBS


def link_target() -> tuple:
    """
    返回 (TargetPath, Arguments) —— .lnk 指向什么、带什么参数。

    两种形态：
      **打包版**：直接指向同目录的 Speak More.exe，不带参数。
        （发行版里没有 启动 Speak More.vbs —— 它不在打包清单里，
          而且 exe 自己就够用了）
      **源码版**：指向 wscript.exe，参数是项目里的 启动 Speak More.vbs。
        包这一层是为了图标：.vbs 直接放 Startup 里显示的是 wscript 的卷轴图标，
        .lnk 才能带 IconLocation。
    """
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve(), ''
    return WSCRIPT, str(launcher_path())


def icon_path() -> Path:
    return project_root() / 'assets' / 'icon.ico'


def is_enabled() -> bool:
    """Startup 里有没有我们的条目（.lnk 和旧版 .vbs 都算）。"""
    return link_path().is_file() or (startup_dir() / LEGACY_VBS).is_file()


# ============================================================
#  写 .lnk
# ============================================================

# 用 PowerShell 的 WScript.Shell COM 建快捷方式 —— 不引 pywin32。
# 参数从环境变量传，避免路径里的引号/中文在命令行上被转义搞乱。
_PS_TEMPLATE = r'''
$ErrorActionPreference = 'Stop'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SM_LNK)
$s.TargetPath       = $env:SM_TARGET
if ($env:SM_ARGS) { $s.Arguments = '"' + $env:SM_ARGS + '"' }
$s.WorkingDirectory = $env:SM_CWD
$s.IconLocation     = $env:SM_ICON + ',0'
# 刻意不设 $s.WindowStyle / $s.Description —— 见模块 docstring 里的坑
$s.Save()
Write-Output 'OK'
'''


def enable() -> Optional[str]:
    """
    装开机自启。成功返回 None，失败返回人话错误说明。

    幂等：已经装好了再调一次也没问题（覆盖写）。
    """
    target, args = link_target()
    if not target.is_file():
        return f'找不到启动器 {target}'

    startup_dir().mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env['SM_LNK'] = str(link_path())
    env['SM_TARGET'] = str(target)
    env['SM_ARGS'] = args
    env['SM_CWD'] = str(project_root())
    env['SM_ICON'] = str(icon_path())

    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
             '-Command', _PS_TEMPLATE],
            capture_output=True, text=True, timeout=60, env=env,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception as e:
        return f'调用 PowerShell 失败: {e}'

    if r.returncode != 0 or not link_path().is_file():
        tail = (r.stderr or r.stdout or '').strip()[-300:]
        return f'创建快捷方式失败: {tail}'

    # 清掉旧版遗留的 .vbs，免得开机起两次
    legacy = startup_dir() / LEGACY_VBS
    if legacy.is_file():
        try:
            legacy.rename(legacy.with_suffix('.vbs.disabled'))
        except Exception:
            pass

    return None


def disable() -> None:
    """关掉开机自启（.lnk 和旧版 .vbs 都删）。文件不存在就什么都不做。"""
    for name in (f'{LINK_NAME}.lnk', LEGACY_VBS):
        p = startup_dir() / name
        try:
            if p.is_file():
                p.unlink()
        except Exception:
            pass


def describe() -> str:
    """给设置面板 / 关于页用的一行状态。"""
    if not is_enabled():
        return '开机自启：未开启'
    if link_path().is_file():
        return f'开机自启：已开启（{link_path().name}）'
    return '开机自启：已开启（旧版 .vbs，建议重装一次以带上图标）'


if __name__ == '__main__':
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if cmd == 'enable':
        err = enable()
        print(f'失败: {err}' if err else '已开启开机自启')
    elif cmd == 'disable':
        disable()
        print('已关闭开机自启')
    else:
        print(describe())
        print(f'  条目：{link_path()}')
        print(f'  启动器：{launcher_path()}')
        print(f'  图标：{icon_path()}')
