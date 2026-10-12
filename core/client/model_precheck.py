# coding: utf-8
"""
客户端启动预检：模型与推理库（llama.cpp DLL）齐不齐。

为什么要在客户端做这件事：
    服务端是被客户端用隐藏控制台拉起来的，缺模型时它只会**静默退出** ——
    日志里留一行、界面上什么都没有。用户看到的现象是"双击了、托盘也有、
    可按住 CapsLock 毫无反应"，既不知道哪里错、也不知道去哪修。

    在客户端启动时先自查一遍，就能把这件事变成一句人话 + 一个能点的按钮。

判断逻辑与 core/server/worker/check_model.py 共用 core.tools.model_paths，
两边结论不会打架。

注意：模型类型取 config_local.json 的 server.model_type，没写就用默认的
qwen_asr（与 config_server.ServerConfig.model_type 的默认值一致）。如果用户
直接手改 config_server.py 换了引擎，这里可能查的不是同一个 —— 但服务端自己
那道检查仍然是权威的，这里只是"提前告知"。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from core.tools.model_paths import (
    ModelDownloadLinks,
    find_missing_dlls,
    find_missing_models,
)

DEFAULT_MODEL_TYPE = 'qwen_asr'
ENGINE_NAMES = {
    'qwen_asr': 'Qwen3-ASR',
    'fun_asr_nano': 'Fun-ASR-Nano',
    'sensevoice': 'SenseVoice',
    'paraformer': 'Paraformer',
}


@dataclass
class PrecheckReport:
    """一次预检的结果。"""
    model_type: str
    supported: bool = True
    model_dir: Optional[Path] = None
    missing_models: List[Path] = field(default_factory=list)
    misplaced: bool = False
    missing_dlls: List[str] = field(default_factory=list)
    download_page: str = ModelDownloadLinks.models_page

    @property
    def ok(self) -> bool:
        return self.supported and not self.missing_models and not self.missing_dlls

    @property
    def has_problems(self) -> bool:
        return not self.ok


def current_model_type() -> str:
    """从 config_local.json 的 server 段取模型类型，没有就用默认值。"""
    try:
        from config_local import get_server_overrides
        value = (get_server_overrides() or {}).get('model_type')
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    except Exception:
        pass
    return DEFAULT_MODEL_TYPE


def run_precheck(root: Optional[Path] = None) -> PrecheckReport:
    """
    跑一遍预检。root 传安装目录（不传就按相对路径检查，依赖 cwd 已是安装目录）。
    本函数不打印、不弹窗、不抛异常。
    """
    model_type = current_model_type()
    try:
        info = find_missing_models(model_type)
        report = PrecheckReport(
            model_type=model_type,
            supported=bool(info.get('supported')),
            model_dir=info.get('model_dir'),
            missing_models=list(info.get('missing') or []),
            misplaced=bool(info.get('misplaced')),
            download_page=info.get('download_page') or ModelDownloadLinks.models_page,
        )
        report.missing_dlls = find_missing_dlls(model_type, root)
        return report
    except Exception:
        # 预检本身出问题绝不能影响启动
        return PrecheckReport(model_type=model_type)


def format_lines(report: PrecheckReport) -> List[str]:
    """把结果整理成给用户看的短句（界面与日志共用）。"""
    if report.ok:
        return []

    lines: List[str] = []
    if not report.supported:
        lines.append(f'模型类型 "{report.model_type}" 不认识，服务端会拒绝启动。')
        return lines

    if report.missing_models:
        lines.append(f'缺少 {len(report.missing_models)} 个模型文件（{ENGINE_NAMES.get(report.model_type, report.model_type)}）：')
        for p in report.missing_models[:5]:
            lines.append(f'    · {p.name}')
        if len(report.missing_models) > 5:
            lines.append(f'    · …等 {len(report.missing_models)} 个')
        if report.model_dir:
            lines.append(f'  应放到：{report.model_dir}')
        if report.misplaced:
            lines.append('  （检测到上级目录里有同名文件 —— 多半是解压时多套了一层文件夹）')

    if report.missing_dlls:
        lines.append(f'缺少 {len(report.missing_dlls)} 个 llama.cpp 运行库：')
        for name in report.missing_dlls[:5]:
            lines.append(f'    · {name}')
        if len(report.missing_dlls) > 5:
            lines.append(f'    · …等 {len(report.missing_dlls)} 个')

    lines.append(f'下载地址：{report.download_page}')
    return lines


def format_text(report: PrecheckReport) -> str:
    return '\n'.join(format_lines(report))


def open_model_dir(report: PrecheckReport) -> None:
    """打开模型目录（不存在就先建出来，用户好往里放）。"""
    target = report.model_dir
    if target is None:
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(str(target))          # noqa: S606 (Windows only)
    except Exception:
        pass


def open_download_page(url: str) -> None:
    try:
        os.startfile(url)                  # 交给默认浏览器  # noqa: S606
    except Exception:
        pass


def _message_box(text: str, title: str = 'Speak More') -> int:
    """
    系统消息框（不依赖 Tk 线程，启动早期也能用）。

    返回：6=是 7=否 2=取消；调用失败返回 0。
    """
    try:
        import ctypes
        MB_YESNOCANCEL = 0x03
        MB_ICONWARNING = 0x30
        MB_TOPMOST = 0x40000
        return int(ctypes.windll.user32.MessageBoxW(
            None, text, title, MB_YESNOCANCEL | MB_ICONWARNING | MB_TOPMOST))
    except Exception:
        return 0


def notify_if_problems(report: PrecheckReport, log) -> bool:
    """
    有缺失就告诉用户。返回是否一切正常。

    - 写一条 WARNING 到客户端日志（排查时唯一入口）
    - 弹一个系统消息框：是 = 打开模型目录，否 = 打开下载页，取消 = 稍后自己弄
    """
    if report.ok:
        return True

    detail = format_text(report)
    try:
        log.warning('启动预检发现问题：\n' + detail)
    except Exception:
        pass

    choice = _message_box(
        'Speak More 暂时还不能用：缺少识别所需的文件。\n\n'
        f'{detail}\n\n'
        '「是」= 打开模型目录（把文件放进去后重启本程序）\n'
        '「否」= 打开下载页面\n'
        '「取消」= 先不管，稍后自己处理')

    if choice == 6:                        # 是
        open_model_dir(report)
    elif choice == 7:                      # 否
        open_download_page(report.download_page)
    return False
