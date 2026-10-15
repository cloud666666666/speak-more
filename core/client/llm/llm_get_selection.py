"""
LLM 获取选中文字功能

两条路，优先走第一条：

  ① **无障碍接口直接读选区**（core/tools/ui_reader.py，Windows UI Automation）
     不发按键、不碰剪贴板；还能明确区分「确实没选中」和「读不了」。
     终端里也安全。

  ② **退路：模拟 Ctrl+C，看剪贴板变没变**（作者原本的做法，保留）。
     只在 ① 读不了（控件不支持）时才用，而且**在终端窗口里绝不用** ——
     那里 Ctrl+C 是 SIGINT，会把前台跑着的进程直接打断。

[补丁] 为什么必须改成这样：原做法在"没选中任何东西"时会拷走整个上下文 ——
VS Code 里 Ctrl+C 会复制**当前整行代码**，剪贴板一变，"内容变了就算选中"
这道判断就通过了，于是整行代码被当成用户选中文字塞进提示词；在终端里
更糟，直接把 SIGINT 送给前台程序（实跑过：会打断 dev server / 构建）。
"""
import time
import pyclip
import keyboard
from . import logger
from .llm_clipboard import safe_paste


# 全局变量：记录每个角色最后一次使用的选中文字
_last_selection_by_role = {}

# 这些前台程序里 Ctrl+C 不是"复制"而是 SIGINT —— 退路方案要避开它们。
# （认不出来的（比如 VS Code 的集成终端，编辑器和终端是同一个进程）就交给
# 上面那条无障碍接口；UIA 读不了又认不出是终端时，才可能误发，概率很低。）
_TERMINAL_PROCS = {
    'windowsterminal.exe', 'openconsole.exe', 'conhost.exe', 'cmd.exe',
    'powershell.exe', 'pwsh.exe', 'mintty.exe', 'bash.exe', 'sh.exe',
    'wsl.exe', 'wezterm-gui.exe', 'alacritty.exe', 'kitty.exe',
    'hyper.exe', 'tabby.exe', 'terminus.exe', 'conemu64.exe',
    'fluentterminal.exe', 'cmder.exe',
}


def get_selected_text(role_config, state) -> str:
    """
    获取用户当前选中的文字

    Args:
        role_config: 角色配置 RoleConfig 对象
        state: ClientState

    Returns:
        选中的文字，如果不应该使用则返回空字符串
    """
    global _last_selection_by_role

    # 检查是否启用获取选中文字
    if not role_config.enable_read_selection:
        return ""

    role_name = role_config.name

    try:
        selected_text = _read_selection(state)
        if not selected_text:
            return ""

        # 检查长度限制
        max_length = getattr(role_config, 'selection_max_length', 1000)
        if len(selected_text) > max_length:
            selected_text = selected_text[:max_length]

        # 如果开启了历史记录，检查选中文字是否与上一次使用的相同
        if role_config.enable_history:
            last_selection = _last_selection_by_role.get(role_name, "")
            if selected_text == last_selection:
                # 选中文字没有变化，且上一次已经使用过，不再加入上下文
                return ""

        # 检查是否只包含空白字符（空格、制表符、换行等）
        if not selected_text.strip():
            return ""

        return selected_text

    except Exception as e:
        logger.warning(f"获取选中文字失败: {e}")
        return ""


def _read_selection(state) -> str:
    """
    取"当前选中的文字"，取不到返回空串。

    ⚠️ 两个来源的判据不同，别混：
      - 无障碍接口说 `''` = **确实没选中东西** → 直接返回空，**不要**再去发 Ctrl+C
        （正是这一步修掉了"没选中就把整行代码当选中文字"的老毛病）；
      - 无障碍接口返回 `None` = 这个控件读不了 → 才走退路。
    """
    try:
        from core.tools import ui_reader

        picked = ui_reader.read_selection()
        if picked is not None:
            if picked.strip() and picked != state.last_output_text:
                logger.debug(f'选中文字：无障碍接口读到 {len(picked)} 字')
                return picked
            logger.debug('选中文字：无障碍接口说没有选中内容')
            return ""
    except Exception as e:
        logger.debug(f'无障碍接口读选区失败，走退路: {e}')

    # ---------- 退路 ----------
    if _is_terminal_window():
        logger.info('当前窗口是终端，跳过「Ctrl+C 读选中文字」'
                    '（在终端里那等于发 SIGINT，会打断前台进程）')
        return ""

    # 保存当前剪贴板内容
    original_clipboard = safe_paste()

    # 模拟 Ctrl+C 复制选中的文字
    keyboard.press_and_release('ctrl+c')

    # 等待复制操作完成
    time.sleep(0.1)

    # 读取新的剪贴板内容
    selected_text = safe_paste()

    # 还原原来的剪贴板内容
    # （单独 try：还原失败不该把已经拿到的选中文字一起丢掉）
    try:
        pyclip.copy(original_clipboard)
    except Exception as e:
        logger.debug(f'还原剪贴板失败（不影响本次取词）: {e}')

    # 如果内容没有变化，说明没有选中文字，返回空字符串
    if selected_text == original_clipboard or selected_text == state.last_output_text:
        return ""
    logger.debug(f'选中文字：Ctrl+C 退路读到 {len(selected_text)} 字')
    return selected_text


def _is_terminal_window() -> bool:
    """当前前台窗口是不是终端（决定能不能用 Ctrl+C 取词）。"""
    try:
        from core.tools.window_detector import get_active_window_info
        name = (get_active_window_info().get('process_name') or '').lower()
        return name in _TERMINAL_PROCS
    except Exception:
        return False


def record_selection_usage(role_config, selection_text: str):
    """
    记录角色使用的选中文字（用于下一轮判断是否重复）

    Args:
        role_config: 角色配置 RoleConfig 对象
        selection_text: 使用的选中文字（空字符串表示没有使用）
    """
    global _last_selection_by_role
    role_name = role_config.name
    _last_selection_by_role[role_name] = selection_text

