# coding: utf-8
"""
读出「当前焦点输入框里的文本 / 选中的文字」—— Windows UI Automation。

两个用途（都在客户端，都只读不写）：

  1. **从你的纠正里学词**（core/client/correction_learner.py）：刚把识别结果打进
     某个应用之后，隔几秒回读同一个输入框，对比出你改了什么，把改动的词收进
     词典（对标 Typeless 的 Auto-added）。
  2. **读「选中的文字」**（core/client/llm/llm_get_selection.py）：翻译 / 大助理这
     类角色要看你现在选中了什么。以前靠"模拟 Ctrl+C + 看剪贴板变没变"，在没
     选中东西时会拷走整个上下文（VS Code 里就是整行代码），在终端里更会把
     SIGINT 送给前台进程。UIA 能直接读选区，还能明确区分「没选中」和「读不了」。

⚠️ 这是「读屏幕」的能力，所以本模块的边界写死在这儿：
  - **只在本地读、本地比对**，绝不外发（本产品全程离线，没有任何上传代码）
  - 只在「刚输出过一段文本」之后的短时间内读一次，**不是常驻监控**
  - 读不到就放弃（很多控件不支持），不做任何降级（不截图、不翻剪贴板、不模拟键盘）
  - 开关在设置面板「常用」页：`learn_from_corrections`，关掉就完全不读

技术说明：用 comtypes 调 UI Automation 的 COM 接口。ValuePattern 覆盖绝大多数
输入框（实测微信的 ChatInputField 可以），读不到时退回 TextPattern（富文本 / 浏览器）。
Tk 控件两种都没有 —— 这功能对别的应用才有意义。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_uia = None            # 缓存的 IUIAutomation 实例（COM 对象，跨线程要 CoInitialize）
_UIA_FAILED = False


def _get_uia():
    """
    拿到 IUIAutomation 实例；不可用就返回 None。

    ⚠️ 必须先在本线程 `CoInitialize` 再 `GetModule`：这个函数第一次被调到的
    地方通常是**后台监视线程**（见 core/client/correction_learner.py），而
    comtypes 在没有初始化 COM 的线程里生成接口会失败。踩过一次 —— 那次失败
    还会被 `_UIA_FAILED` 永久缓存下来，之后连主线程都拿不到了。
    所以"本线程没起来 COM"这种情况**不缓存失败**，下次换个线程还会再试。
    """
    global _uia, _UIA_FAILED
    if _uia is not None or _UIA_FAILED:
        return _uia
    if not _ensure_com():
        return None
    try:
        import comtypes.client

        comtypes.client.GetModule('UIAutomationCore.dll')
        from comtypes.gen import UIAutomationClient as UIA  # noqa: N811

        _uia = comtypes.client.CreateObject(UIA.CUIAutomation,
                                            interface=UIA.IUIAutomation)
    except Exception:
        # 到这里才是"真的用不了"（没装 comtypes、系统不给接口），缓存住别再试
        _UIA_FAILED = True
        _uia = None
    return _uia


def _text_of(el: Any) -> Optional[str]:
    """从一个 UIA 元素上取文本（ValuePattern 优先，退回 TextPattern）。"""
    from comtypes.gen import UIAutomationClient as UIA  # noqa: N811

    try:
        vp = el.GetCurrentPattern(UIA.UIA_ValuePatternId).QueryInterface(
            UIA.IUIAutomationValuePattern)
        value = vp.CurrentValue
        if isinstance(value, str):
            return value
    except Exception:
        pass
    try:
        tp = el.GetCurrentPattern(UIA.UIA_TextPatternId).QueryInterface(
            UIA.IUIAutomationTextPattern)
        value = tp.DocumentRange.GetText(-1)
        if isinstance(value, str):
            return value
    except Exception:
        pass
    return None


def read_selection_from(el: Any) -> Optional[str]:
    """
    读某个元素上**被选中**的那段文字。

    返回值有三态，调用方要分清：
      - `None`  → 这个控件不支持读选区（没有 TextPattern）
      - `''`    → 支持读，但**当前没有选中任何东西**（只有光标）
      - 非空串  → 选中的文字

    为什么这件事重要：模拟 Ctrl+C 取词在"没选中东西"时会把整个上下文
    （VS Code 里就是整行代码）拷出来当成选中文字；而 UI Automation 能明确
    告诉我们"确实没选中"（`''`），于是不必去发那个按键。
    """
    from comtypes.gen import UIAutomationClient as UIA  # noqa: N811

    try:
        tp = el.GetCurrentPattern(UIA.UIA_TextPatternId).QueryInterface(
            UIA.IUIAutomationTextPattern)
    except Exception:
        return None                       # 控件没有文本接口
    try:
        ranges = tp.GetSelection()        # 选区可能有多段
        if ranges is None:
            return ''
        parts = []
        for i in range(ranges.Length):
            text = ranges.GetElement(i).GetText(-1)
            if isinstance(text, str):
                parts.append(text)
        return ''.join(parts)
    except Exception:
        return None


def read_selection() -> Optional[str]:
    """
    读当前焦点控件里被选中的文字（三态同上：None=读不了 / ''=没选东西）。

    ⚠️ 有一道自检：某些控件会把**整篇文档**当成"选中内容"报上来
    （实测：**没获得焦点**的记事本 RichEditD2DPT 就是这样）。真要是信了它，
    用户随口一句"翻译"，整篇文档就被塞进提示词了 —— 比原来 Ctrl+C 那套还糟。
    所以"选区"和"整框文本"一模一样时，一律按"没有选中内容"处理
    （代价：用户 Ctrl+A 全选整篇时我们不当作选区，这种情况很少，且不报错）。
    """
    if not _ensure_com():
        return None
    uia = _get_uia()
    if uia is None:
        return None
    try:
        el = uia.GetFocusedElement()
        if el is None:
            return None
        selection = read_selection_from(el)
        if selection is None:
            return None
        if _looks_like_whole_text(selection, _text_of(el)):
            return ''                     # 不可信，按"没选中"处理
        return selection
    except Exception:
        return None


def _looks_like_whole_text(selection: Optional[str],
                           whole: Optional[str]) -> bool:
    """报上来的"选区"是不是跟整框文本一模一样（那多半是控件在骗人）。"""
    return bool(selection) and bool(whole) and selection == whole


def read_focused_text() -> Optional[str]:
    """
    读当前焦点元素里的文本。读不到（控件不支持 / UIA 不可用）返回 None。

    ⚠️ 调用前请确保 COM 已在本线程初始化（`_ensure_com()`）；本函数自己也会试一次。
    """
    if not _ensure_com():
        return None
    uia = _get_uia()
    if uia is None:
        return None
    try:
        el = uia.GetFocusedElement()
        if el is None:
            return None
        return _text_of(el)
    except Exception:
        return None


def focused_info() -> Dict[str, Any]:
    """焦点元素的基本信息（给日志用：知道刚才读的是哪个控件）。"""
    info: Dict[str, Any] = {}
    if not _ensure_com():
        return info
    uia = _get_uia()
    if uia is None:
        return info
    try:
        el = uia.GetFocusedElement()
        if el is None:
            return info
        info['name'] = el.CurrentName
        info['class'] = el.CurrentClassName
        info['control_type'] = el.CurrentControlType
    except Exception:
        pass
    return info


def available() -> bool:
    """UI Automation 能不能用（comtypes 是否装好、接口能否生成）。"""
    return _get_uia() is not None


def _ensure_com() -> bool:
    """本线程初始化 COM（在工作线程里调 UIA 必须做）。"""
    try:
        import comtypes

        comtypes.CoInitialize()
        return True
    except Exception:
        return False
