# coding: utf-8
"""
统一的应用图标

问题：托盘图标是我们自己的 `assets/icon.ico`（Speak More 的圆角方块 + 波形柱），
但其它地方全是系统默认的 —— Tk 窗口标题栏和任务栏是 Tk 自带的羽毛，
黑窗口的标题栏更是 python.exe 的图标。用户看到的就是"图标不统一，
有的地方还是 Python 自带的"。

这个模块把所有能设图标的地方都指向同一份 `assets/icon.ico`：

  ┌──────────────────────┬───────────────────────────────────────────┐
  │ Tk 窗口（设置面板、    │ `apply_to_window()`  -> WM_SETICON         │
  │ 向导、上下文/热词框）   │  一次设好标题栏 + 任务栏 + Alt+Tab 三处     │
  ├──────────────────────┼───────────────────────────────────────────┤
  │ 控制台黑窗口           │ `apply_to_console()` -> 同一个函数          │
  ├──────────────────────┼───────────────────────────────────────────┤
  │ 任务栏分组身份         │ `set_taskbar_identity()` -> AppUserModelID │
  └──────────────────────┴───────────────────────────────────────────┘

## 为什么直接发 WM_SETICON，而不是 `wm iconbitmap`

**Tk 8.6 在 Windows 上把 `wm iconbitmap` 做成了彻底的 no-op** —— 不抛异常、
不生效、连值都不记。实测（Tk 8.6.14）：

    w.iconbitmap(r'...\\icon.ico')    # 不抛任何异常
    w.wm_iconbitmap()                 # -> ''    值都没存下来
    SendMessageW(hwnd, WM_GETICON)    # -> 0     窗口上什么都没有

8.6 在 Windows 上给的替代品是 `wm iconphoto`，但要先把 .ico 转成
`PhotoImage`（Tk 读不了 .ico），而**实测它同样不改 WM_GETICON** ——
任务栏吃不吃得到并不确定。更要命的是它引入了一个死锁：

    ImageTk.PhotoImage 若在设置面板那个 Tk 线程的解释器里创建，
    主线程 GC 跑它的 __del__ 时会去碰正在 mainloop 的解释器 ——
    **进程从此退不出去**。和 tkinter.Variable.__del__ 是同一个坑
    （见 core/ui/settings_dialog.py 的 _forget_tk_vars）。

所以这里不用 Tk 那套 API，直接对窗口句柄发 WM_SETICON。实测 WM_GETICON
立刻返回我们自己 LoadImageW 出来的 HICON —— 这是**能自动验证**的唯一做法，
`test_app_icon.py` 就是这么测的（不是截图比对）。

## 为什么必须有 AppUserModelID

Windows 任务栏按钮默认继承**宿主进程 exe** 的身份。我们用 python.exe 跑，
于是任务栏认为"这是 Python"：窗口自己没设图标时直接拿 Python 的图标顶上，
pin 到任务栏之后更是永远一个 Python 图标。
`SetCurrentProcessExplicitAppUserModelID` 把身份改成 `SpeakMore.Client`，
任务栏才会老实采用窗口自己的图标。**必须在建任何窗口之前调用**，
所以它在 `app.start()` 的最前面。

## 硬性约束

  - Windows 专属。这个分发本来就是 Windows 专用的（托盘依赖控制台 HWND、
    启动器是 .vbs/.bat），其它平台直接返回 False，不做半吊子适配。
  - 图标文件找不到、API 不存在、任何异常 —— 一律吞掉返回 False。
    图标丑是小事，起不来是大事。
  - 不依赖 PIL。托盘那边要 PIL 是因为 pystray 只吃 PIL Image；
    这里全走系统 API，与 Tk / PIL / 解释器生命周期完全无关，
    因此**不会**再出现"进程退不出去"那类跨线程解释器死锁。
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from typing import Optional

_IS_WINDOWS = os.name == 'nt'

# 进程级只做一次的事
_console_done = False
_taskbar_identity_done = False


# ============================================================
#  Win32 动态库句柄
# ============================================================
#
# 用自己持有的 WinDLL 而不是 ctypes.windll.user32：后者是进程级共享的，
# 参数类型被别人改过就会踩到我们（比如某个库把 SendMessageW 的 restype
# 声明成 c_int，64 位下 HICON 会被截断）。这里每次都显式声明签名。

_user32_dll = None
_kernel32_dll = None
_shell32_dll = None


def _user32():
    global _user32_dll
    if _user32_dll is None:
        u = ctypes.WinDLL('user32', use_last_error=True)
        # 默认 restype 是 c_int，64 位下句柄会被截断成负数
        u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_void_p, ctypes.c_void_p]
        u.SendMessageW.restype = ctypes.c_void_p
        u.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        u.LoadImageW.restype = ctypes.c_void_p
        u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        u.GetAncestor.restype = ctypes.c_void_p
        u.SetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        u.SetClassLongPtrW.restype = ctypes.c_void_p
        _user32_dll = u
    return _user32_dll


def _kernel32():
    global _kernel32_dll
    if _kernel32_dll is None:
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.GetConsoleWindow.restype = ctypes.c_void_p
        _kernel32_dll = k
    return _kernel32_dll


def _shell32():
    global _shell32_dll
    if _shell32_dll is None:
        s = ctypes.WinDLL('shell32', use_last_error=True)
        s.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
        s.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        _shell32_dll = s
    return _shell32_dll


# ============================================================
#  定位图标文件
# ============================================================

def icon_path() -> Optional[Path]:
    """
    找到 `assets/icon.ico`。

    搜索顺序：
      1. 冻结包（PyInstaller）里 `__file__` 在 _MEIPASS 下，得用 exe 所在目录
      2. 本文件往上两级（core/ui/app_icon.py -> 项目根）
      3. 当前工作目录

    一律返回**绝对路径**：Tk / LoadImageW 按 cwd 解析相对路径，而这里可能是
    被托盘回调（pystray 消息线程）触发的，cwd 未必是安装根 ——
    踩过"图标时有时无"。

    找不到返回 None，调用方一律当"没图标"处理。
    """
    bases = []

    try:
        if getattr(sys, 'frozen', False):
            bases.append(Path(sys.executable).resolve().parent)
    except Exception:
        pass

    try:
        bases.append(Path(__file__).resolve().parents[2])
    except Exception:
        pass

    try:
        bases.append(Path.cwd())
    except Exception:
        pass

    for base in bases:
        try:
            p = base / 'assets' / 'icon.ico'
            if p.is_file():
                return p
        except Exception:
            continue
    return None


# ============================================================
#  找对窗口句柄（这一步错了后面全白干）
# ============================================================

_GA_ROOT = 2

# ⚠️ **Tk 的 winfo_id() 不是画标题栏的那个窗口。**

# 实测（Tk 8.6.14 / Windows 11）：
#
#   tk.Toplevel() 的 winfo_id() -> class=TkChild, style 带 WS_CHILD,
#                                  GetParent() = 真正的框架窗口, GA_ROOT != 它自己
#
# 也就是说 winfo_id() 给的是**框架内部的子窗口**，标题栏、任务栏图标、
# Alt+Tab 三者全都属于外面那个框架窗口。往子窗口发 WM_SETICON 的后果极其
# 具有迷惑性：`WM_GETICON` 读回来是**非 0**（确实设上了），但屏幕上一丝变化
# 都没有 —— 我一开始的测试就是这么"通过"的，直到把标题栏截图出来比对才发现
# 两个窗口画的都是 Tk 的羽毛。
#
# 所以必须先 `GetAncestor(hwnd, GA_ROOT)` 拿到顶层框架再设。
# （对 `tk.Tk()` 根窗口，winfo_id() 本身就是顶层，GetAncestor 会原样返回它。）


def top_level_hwnd(win) -> int:
    """
    返回窗口**真正带标题栏**的那个顶层 HWND。

    拿不到就退回 winfo_id() 本身（至少是原来那个行为）。失败返回 0。
    """
    try:
        hwnd = int(win.winfo_id())
    except Exception:
        return 0
    if not hwnd:
        return 0
    try:
        root = _user32().GetAncestor(ctypes.c_void_p(hwnd), _GA_ROOT)
        if root:
            return int(root)
    except Exception:
        pass
    return hwnd


# ============================================================
#  真正的干活函数：对窗口句柄发 WM_SETICON
# ============================================================

# LoadImageW 的 fuLoad / 类型
_LR_LOADFROMFILE = 0x00000010
_IMAGE_ICON = 1

_WM_SETICON = 0x0080
_WM_GETICON = 0x007F
_ICON_SMALL = 0
_ICON_BIG = 1

# SetClassLongPtrW 的索引（GetClassLongPtr 用同一套）
_GCLP_HICON = -14
_GCLP_HICONSM = -34

# GetSystemMetrics 的索引
_SM_CXICON, _SM_CYICON = 11, 12
_SM_CXSMICON, _SM_CYSMICON = 49, 50

# HICON 的所有权在我们手上 —— 系统不接管，销毁早了图标就没了。
# 挂到模块级列表里保命，进程退出时随进程一起消失（无需也不该 DestroyIcon）。
_hicons: list = []


def _set_window_icons(hwnd: int, path: Path) -> bool:
    """
    对 hwnd 发 WM_SETICON，大/小两个尺寸都设，顺带改窗口类的图标。

    两个都要设：`ICON_SMALL` 管标题栏左上角，`ICON_BIG` 管任务栏和 Alt+Tab。
    只设一个的话另一个会回落到默认图标，看着还是"不统一"。

    尺寸必须显式取系统度量再传给 LoadImageW：传 0,0 + LR_DEFAULTSIZE 在高
    DPI 下会给错的大小，16×16 那个会被拉伸糊掉。

    ## 为什么要连窗口类图标（GCLP_HICON）一起改

    Tk 的默认羽毛**不是**通过 WM_SETICON 设的，而是设在这个窗口类上的。
    实测一个刚建好的 Toplevel：

        child(winfo_id) WM_GETICON=0  GCLP_HICON=0
        frame(GA_ROOT)  WM_GETICON=0  GCLP_HICON=63310245   <- Tk 的羽毛在这

    只发 WM_SETICON 其实已经能盖住标题栏（WM_GETICON 优先级高于类图标），
    但**改类图标能一次覆盖本进程所有 Tk 顶层窗口**，包括我们不认识的那些
    （Tk 自己弹的内部窗口）。所以两个都做。

    对别的进程的窗口（控制台是 conhost 的）改类图标会失败，属正常，
    静默跳过即可 —— WM_SETICON 那部分照样生效。
    """
    if not hwnd or not _IS_WINDOWS:
        return False

    try:
        user32 = _user32()
        sizes = (
            (_ICON_BIG, user32.GetSystemMetrics(_SM_CXICON),
             user32.GetSystemMetrics(_SM_CYICON)),
            (_ICON_SMALL, user32.GetSystemMetrics(_SM_CXSMICON),
             user32.GetSystemMetrics(_SM_CYSMICON)),
        )

        done = False
        for which, cx, cy in sizes:
            hicon = user32.LoadImageW(None, str(path), _IMAGE_ICON,
                                      cx, cy, _LR_LOADFROMFILE)
            if not hicon:
                continue
            _hicons.append(hicon)
            user32.SendMessageW(hwnd, _WM_SETICON, which, hicon)

            # 顺带改窗口类图标。只对**本进程**的窗口有效，跨进程会失败
            # （控制台就是 conhost 的窗口），失败无所谓。
            try:
                cls_which = _GCLP_HICON if which == _ICON_BIG else _GCLP_HICONSM
                user32.SetClassLongPtrW(hwnd, cls_which, hicon)
            except Exception:
                pass

            done = True
        return done
    except Exception:
        return False


def window_icon_handle(hwnd: int, big: bool = True) -> int:
    """
    读回窗口当前的图标句柄。**专门给测试用的**。

    返回 0 表示窗口上没设过图标 —— Windows 就会回落到窗口类的默认图标
    （Tk 的羽毛 / conhost 的 python.exe 图标），也就是用户看到"图标不统一"
    的原因。所以这是唯一能自动判定的证据，比截图比对靠谱。

    也能跨进程查（SendMessage 会跨进程送到对方窗口上），
    所以可以用它去验证**正在运行的那个客户端**有没有设上图标。
    """
    if not hwnd or not _IS_WINDOWS:
        return 0
    try:
        got = _user32().SendMessageW(hwnd, _WM_GETICON,
                                     _ICON_BIG if big else _ICON_SMALL, 0)
        return int(got or 0)      # restype 是 c_void_p，0 会回来成 None
    except Exception:
        return 0


# ============================================================
#  Tk 窗口
# ============================================================

def apply_to_window(win) -> bool:
    """
    给一个 Tk / Toplevel 窗口设图标。返回是否设上了。

    **注意找的是顶层框架窗口**（见 `top_level_hwnd()`）—— 直接拿
    `winfo_id()` 去设会把图标设在一个看不见的子窗口上，读回来非 0 但
    屏幕上毫无变化。

    在窗口 `withdraw()` 状态下调用也没问题：设的是窗口属性，
    `deiconify()` 之后自然带上 —— 所以可以紧跟 `title()` 之后调，
    不必等窗口显示出来（那时候再设会看见图标闪一下）。

    失败只吞掉（无头环境、非 Windows、图标文件缺失），绝不抛。
    """
    if win is None or not _IS_WINDOWS:
        return False

    path = icon_path()
    if path is None:
        return False

    # **必须先逼 Tk 把窗口真正建出来。**
    # 刚 `tk.Toplevel()` 完还没 update 的时候，Tk 只建了内部子窗口，
    # 顶层框架（TkTopLevel）还不存在 —— 此时 GetAncestor(GA_ROOT) 会
    # 原样返回那个子窗口，图标就设到了用户看不见的地方（WM_GETICON 读回来
    # 非 0，标题栏纹丝不动）。`create_modal_dialog()` 就是这样：建完立刻
    # withdraw，从没 update 过。
    try:
        win.update_idletasks()
    except Exception:
        pass

    hwnd = top_level_hwnd(win)
    if not hwnd:
        return False

    return _set_window_icons(hwnd, path)


def apply_to_root(root) -> bool:
    """
    给 Tk 根窗口设图标。

    ⚠️ **Toplevel 不会继承根窗口的图标**，每个可见窗口都得自己设一遍。
    所以子窗口要用 `apply_to_window()`，不要指望设了 root 就都好了。
    """
    return apply_to_window(root)


# ============================================================
#  控制台窗口
# ============================================================

def apply_to_console() -> bool:
    """
    把黑窗口（控制台）标题栏左上角那个 python.exe 图标换掉。

    为什么需要：托盘的「👁️ 显示/隐藏」会把这个控制台亮出来，用户看到一个
    标题写着 Speak More、图标却是 Python 的黑框，很违和。

    ⚠️ 不能用 `SetConsoleTitle` 那一类去绕 —— 图标来自控制台宿主（conhost）
    的窗口类，只能对 hwnd 发 WM_SETICON 覆盖它。

    这个函数的**输出是幂等的**（`_console_done` 挡着）：重复调用不重复
    LoadImageW，免得白攒一堆 HICON。

    没有控制台（pythonw / CREATE_NO_WINDOW 启动）时静默返回 False。
    """
    global _console_done
    if _console_done or not _IS_WINDOWS:
        return False
    _console_done = True

    path = icon_path()
    if path is None:
        return False

    try:
        hwnd = _kernel32().GetConsoleWindow()
    except Exception:
        return False
    if not hwnd:
        return False

    return _set_window_icons(int(hwnd), path)


# ============================================================
#  任务栏身份
# ============================================================

APP_USER_MODEL_ID = 'SpeakMore.Client'


def set_taskbar_identity(app_id: str = APP_USER_MODEL_ID) -> bool:
    """
    把进程的 AppUserModelID 改成自己的。返回是否成功（成功 = 返回 0）。

    不设的话 Windows 任务栏会按**进程可执行文件**归类 —— 也就是
    `python.exe`。后果有两个：
      1. 窗口自己没设图标时，任务栏直接显示 Python 的图标；
      2. 用户把窗口 pin 到任务栏，pin 的其实是 python.exe，图标永远是 Python。

    幂等（`_taskbar_identity_done`），**必须在创建任何窗口之前调用**，
    否则已经建好的任务栏按钮改不过来。
    """
    global _taskbar_identity_done
    if _taskbar_identity_done or not _IS_WINDOWS:
        return False
    _taskbar_identity_done = True

    try:
        return _shell32().SetCurrentProcessExplicitAppUserModelID(app_id) == 0
    except Exception:
        return False


# ============================================================
#  一把梭
# ============================================================

def apply_everything() -> None:
    """
    客户端启动时调一次：任务栏身份 + 控制台图标。

    Tk 窗口的图标**不在这里做** —— 那些窗口是懒建的（点托盘才弹），
    各自在 `_build()` 里调 `apply_to_window()` 更靠谱。
    """
    set_taskbar_identity()
    apply_to_console()
