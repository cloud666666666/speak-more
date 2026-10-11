# coding: utf-8
"""
服务端进程的生命周期管理（由客户端接管）

作者的 C/S 架构里服务端和客户端是两个独立进程，各自带一个托盘图标。
本补丁把服务端的所有权收归客户端：

  - 客户端启动时确保服务端在跑（不在就拉起来）
  - 客户端退出时把服务端一起收掉
  - 服务端自己的托盘图标关掉（见 config_local.PATCH_SERVER_DEFAULTS）

这样托盘里只剩一个图标，语义上也符合"一个应用"的直觉。

**识别服务端靠监听端口，不靠进程名** —— 进程名在冻结发行版里是
`start_server.exe`，源码跑是 `python.exe`，靠名字会写死也会误伤。

服务端**异步**拉起（后台线程），因为 `ResultProcessor.start()` 本身
就是每 2 秒重试的连接循环（result_processor.py:159-165），
不需要阻塞客户端启动去等它，也就不会拖慢托盘图标出现。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Set, Tuple

from . import logger

# 只在拉起服务端时用得到，导入失败也不影响其它功能
try:
    import psutil
except Exception:                              # pragma: no cover
    psutil = None                              # type: ignore[assignment]


_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
_ensure_lock = threading.Lock()


# ============================================================
#  基本信息
# ============================================================

def base_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def server_port() -> int:
    try:
        from config_client import ClientConfig as Config
        return int(str(getattr(Config, 'port', '6016')))
    except Exception:
        return 6016


def server_host() -> str:
    try:
        from config_client import ClientConfig as Config
        addr = str(getattr(Config, 'addr', '127.0.0.1') or '127.0.0.1')
        # 客户端连的是 addr，服务端如果绑 0.0.0.0 我们探测时要用回环地址
        return '127.0.0.1' if addr in ('0.0.0.0', '') else addr
    except Exception:
        return '127.0.0.1'


# ============================================================
#  探测
# ============================================================

def find_pids() -> Set[int]:
    """找出正在监听服务端端口的进程。psutil 不可用时返回空集合。"""
    if psutil is None:
        return set()
    port = server_port()
    pids: Set[int] = set()
    try:
        for conn in psutil.net_connections(kind='tcp'):
            try:
                if (conn.status == psutil.CONN_LISTEN
                        and conn.laddr and conn.laddr.port == port
                        and conn.pid):
                    pids.add(conn.pid)
            except Exception:
                continue
    except Exception as e:
        logger.debug(f'枚举端口 {port} 的监听进程失败: {e}')
    return pids


def is_running() -> bool:
    return bool(find_pids())


def describe() -> str:
    """给托盘菜单/日志看的短描述。"""
    pids = find_pids()
    if not pids:
        return '服务端未运行'
    return f'服务端运行中（PID {", ".join(str(p) for p in sorted(pids))}）'


# ============================================================
#  启动
# ============================================================

def _server_command() -> Tuple[Optional[List[str]], Path]:
    """
    返回 (启动命令, 工作目录)。找不到入口时命令为 None。

    冻结发行版用同目录的 start_server.exe；源码跑用当前解释器 + start_server.py。
    """
    base = base_dir()

    if getattr(sys, 'frozen', False):
        exe = Path(sys.executable).resolve().parent / 'start_server.exe'
        if exe.exists():
            return [str(exe)], exe.parent
        logger.error(f'找不到服务端入口: {exe}')
        return None, base

    script = base / 'start_server.py'
    if script.exists():
        return [sys.executable, str(script)], base
    logger.error(f'找不到服务端入口: {script}')
    return None, base


def _kill_orphan_workers() -> int:
    """
    清理上一次服务端崩溃留下的 ASR worker 孤儿。

    为什么需要：ASR worker 是 multiprocessing spawn 出来的独立进程。
    服务端主进程要是被强杀（任务管理器结束任务 / 主进程崩溃），
    Windows 不会连带杀掉子进程 —— 那些孤儿会继续占着
    1.4GB 内存和 1.7GB 显存，而且没有任何界面能看到它们。

    识别条件收紧到三条同时满足，避免误伤别的程序的 multiprocessing 进程：
      1. 命令行里有 'spawn_main'
      2. 工作目录就是本程序的安装目录
      3. 父进程已经不存在了
    """
    if psutil is None:
        return 0

    target_cwd = str(base_dir())
    killed = 0
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'ppid']):
        try:
            info = proc.info
            if not (info['name'] or '').lower().startswith('python'):
                continue

            cmdline = ' '.join(info['cmdline'] or [])
            if 'spawn_main' not in cmdline:
                continue

            ppid = info.get('ppid')
            if ppid and psutil.pid_exists(ppid):
                continue                      # 父进程还活着，不是孤儿

            try:
                if str(proc.cwd()) != target_cwd:
                    continue
            except psutil.Error:
                continue                      # 读不到工作目录就不动它

            proc.kill()
            killed += 1
            logger.info(f'清理了一个残留的识别子进程 (PID {info["pid"]})')
        except psutil.Error:
            continue
        except Exception:
            continue

    return killed


def ensure(timeout: float = 30.0) -> bool:
    """
    确保服务端在运行。已在跑就直接复用；不在就拉起来并等端口就绪。

    Returns:
        True 表示端口已经可用。
    """
    if is_running():
        logger.info('服务端已在运行，直接复用')
        return True

    if psutil is None:
        logger.warning('psutil 不可用，无法确认服务端状态；'
                       '如果你是用启动脚本同时拉起两个进程的，可以忽略这条')
        return False

    # 走到这里说明端口是空的，那上一次服务端要么没起过、要么已经死了。
    # 先收拾掉它可能留下的识别子进程孤儿，再起新的。
    try:
        _kill_orphan_workers()
    except Exception as e:
        logger.debug(f'清理残留子进程时出错（不影响启动）: {e}')

    cmd, base = _server_command()
    if not cmd:
        return False

    # stdout/stderr 必须重定向到文件：
    # rich 对隐藏控制台做 ANSI 光标控制会让 asyncio 事件循环死锁
    # （见 core/server/connection/ws_recv.py 里那个每次收音频都起的 spinner）
    log_path = base / 'logs' / 'server_console.log'
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    kwargs = dict(cwd=str(base), stdin=subprocess.DEVNULL,
                  creationflags=_CREATE_NO_WINDOW, close_fds=True)
    proc = None
    handle = None
    try:
        handle = open(log_path, 'a', encoding='utf-8')
        proc = subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT, **kwargs)
    except Exception as e:
        logger.error(f'拉起服务端失败: {e}', exc_info=True)
        return False
    finally:
        # 子进程已经有自己的句柄副本了，父进程这份可以关掉
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    logger.info(f'已拉起服务端 (PID {proc.pid})，等待端口 {server_port()} 就绪...')

    start = time.time()
    while time.time() - start < timeout:
        if is_running():
            logger.info(f'服务端已就绪，耗时 {time.time() - start:.1f}s')
            return True
        if proc.poll() is not None:
            logger.error(
                f'服务端启动后立即退出（返回码 {proc.returncode}）。'
                f'详情见 {log_path}')
            return False
        time.sleep(0.5)

    logger.error(f'等待服务端端口 {server_port()} 就绪超时（{timeout}s）')
    return False


def ensure_async(timeout: float = 30.0) -> None:
    """后台线程里跑 ensure()，不阻塞调用方（客户端启动路径用这个）。"""
    def _run() -> None:
        with _ensure_lock:
            try:
                ensure(timeout=timeout)
            except Exception as e:
                logger.error(f'后台拉起服务端时出错: {e}', exc_info=True)

    threading.Thread(target=_run, daemon=True, name='EnsureServer').start()
    start_watchdog()


# ============================================================
#  看门狗
# ============================================================

_watchdog_started = False
_watchdog_stop = threading.Event()
_watchdog_lock = threading.Lock()

WATCHDOG_INTERVAL = 15.0     # 每 15 秒探一次
WATCHDOG_MISSES = 2          # 连续两次探不到才动手，避免和「重启服务端」打架


def start_watchdog(interval: float = WATCHDOG_INTERVAL) -> None:
    """
    后台看门狗：服务端意外退出时自动把它拉回来。

    没有它的话服务端一崩，客户端的重连循环（ResultProcessor.start）
    只会一直重试 WebSocket 却永远连不上 —— 表现就是"按住说话没反应"，
    而用户完全不知道为什么。这正是最该避免的隐性失败。

    客户端正常退出时会先 set _watchdog_stop，所以不会和关闭流程打架。
    """
    global _watchdog_started
    with _watchdog_lock:
        if _watchdog_started and not _watchdog_stop.is_set():
            return
        # 之前被 stop() 关过（比如「重启服务端」流程），这里要让它复活
        _watchdog_stop.clear()
        _watchdog_started = True

    def _loop() -> None:
        misses = 0
        while not _watchdog_stop.wait(interval):
            try:
                if is_running():
                    misses = 0
                    continue
                misses += 1
                if misses < WATCHDOG_MISSES:
                    continue
                misses = 0
                logger.warning('看门狗：检测到服务端已退出，正在自动拉起...')
                with _ensure_lock:
                    if not is_running():
                        ensure(timeout=30.0)
            except Exception as e:
                logger.debug(f'看门狗检查出错: {e}')

    threading.Thread(target=_loop, daemon=True, name='ServerWatchdog').start()


# ============================================================
#  停止 / 重启
# ============================================================

def stop(timeout: float = 8.0) -> bool:
    """
    停掉服务端（含它的子进程）。

    必须先收子进程：ASR worker 是 multiprocessing 出来的独立进程，
    只杀父进程会留下一个占着 1.4GB 内存 / 1.7GB 显存的孤儿。
    """
    if psutil is None:
        logger.warning('psutil 不可用，无法停止服务端')
        return False

    # 先让看门狗闭嘴：不然它会在我们停掉服务端之后又把它拉起来
    _watchdog_stop.set()

    pids = find_pids()
    if not pids:
        return True

    targets = []
    for pid in pids:
        try:
            proc = psutil.Process(pid)
        except psutil.Error:
            continue

        try:
            children = proc.children(recursive=True)
        except psutil.Error:
            children = []

        for child in children:
            try:
                child.terminate()
            except psutil.Error:
                pass
        try:
            proc.terminate()
        except psutil.Error:
            pass
        targets.extend(children)
        targets.append(proc)

    if not targets:
        return True

    logger.info(f'正在停止服务端（{len(targets)} 个进程）...')
    _, alive = psutil.wait_procs(targets, timeout=timeout)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass

    deadline = time.time() + 3.0
    while time.time() < deadline and is_running():
        time.sleep(0.2)

    ok = not is_running()
    logger.info('服务端已停止' if ok else '服务端仍有进程残留在监听端口')
    return ok


def restart_async(timeout: float = 30.0) -> None:
    """后台线程里「停掉再拉起」，托盘菜单用这个。"""
    def _run() -> None:
        with _ensure_lock:
            try:
                stop()
                ensure(timeout=timeout)
                start_watchdog()        # 重启不算关闭，看门狗要重新值班
            except Exception as e:
                logger.error(f'重启服务端时出错: {e}', exc_info=True)

    threading.Thread(target=_run, daemon=True, name='RestartServer').start()


def stop_async() -> None:
    threading.Thread(target=stop, daemon=True, name='StopServer').start()
