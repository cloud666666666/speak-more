# coding: utf-8
"""
事件处理器

处理键盘和鼠标事件的逻辑
"""

import time
from . import logger



class ShortcutEventHandler:
    """
    快捷键事件处理器

    处理按键按下和释放的逻辑，包括录音启动、取消、完成等
    """

    def __init__(self, tasks, pool, emulator):
        """
        初始化事件处理器

        Args:
            tasks: 快捷键任务字典
            pool: 线程池
            emulator: 快捷键模拟器
        """
        self.tasks = tasks
        self.pool = pool
        self.emulator = emulator

    def handle_keydown(self, key_name, task) -> None:
        """处理按键按下事件"""
        # 长按模式
        if task.shortcut.hold_mode:
            if not task.is_recording:
                task.launch()
            return

        # [补丁] 单击模式：点一下开始，再点一下结束。
        #
        # 作者原本要走 _count_down / _manage_task 那套阈值判定，但那里有个
        # 逻辑错误：_count_down 要等 threshold(0.3s) 才 set event，
        # 而 _manage_task 只等 threshold*0.8(0.24s) —— 事件永远等不到，
        # 于是第一次按下总是走 else 分支被 task.cancel() 掉。
        #
        # 结果就是：**必须按住超过 0.3 秒**才算开始录音，轻轻点一下什么都不发生。
        # 而用户对"点一下开始"的预期就是轻轻点一下 —— 点了没反应，自然
        # 就以为这个模式坏了、只能用长按。
        #
        # 改成直白的开关语义：没在录音时按下就开始（松手不停），
        # 正在录音时按下就结束并上屏。
        #
        # [补丁] 顺手挡掉**自动重复**，否则这个模式会自己把自己打成筛子：
        # 按住不放时 Windows 每 ~30ms 送一次 keydown，上面那套开关语义会把
        # 每一次重复都当成"再点一下"，于是录音以 30ms 为周期疯狂开停 ——
        # 每个会话 0.03 秒、识别结果为空，而空结果又会一路走到 LLM 和上屏
        # （实测：用户文档里被塞了十几遍模型的客套话）。
        # "再点一下"本来就该靠 keyup 区分：上一次按下之后松过手，才算新的一次点击。
        if task.pressed:
            return
        task.pressed = True

        if task.is_recording:
            task.finish()
        else:
            task.launch()

    def handle_keyup(self, key_name, task) -> None:
        """处理按键释放事件"""
        # 单击模式：开/关都已经在 handle_keydown 里决定了，松手不做任何事
        if not task.shortcut.hold_mode:
            task.pressed = False
            task.released = True
            return

        # 长按模式
        if not task.is_recording:
            return

        duration = time.time() - task.recording_start_time
        logger.debug(f"[{key_name}] 松开，持续时间: {duration:.2f}s")

        if duration < task.threshold:
            self._handle_short_press(key_name, task)
        else:
            task.finish()

    def _handle_short_press(self, key_name, task) -> None:
        """处理短按情况"""
        cancel_start = time.perf_counter()
        task.cancel()
        cancel_time = (time.perf_counter() - cancel_start) * 1000
        logger.debug(f"[{key_name}] task.cancel() 耗时: {cancel_time:.2f}ms")

        if task.shortcut.suppress:
            logger.debug(f"[{key_name}] 安排异步补发按键")
            self.pool.submit(self.emulator.emulate_key, key_name)

    def _count_down(self, task) -> None:
        """倒计时（单击模式）"""
        time.sleep(task.threshold)
        task.event.set()

    def _manage_task(self, task) -> None:
        """管理录音任务（单击模式）"""
        was_recording = task.is_recording

        if not was_recording:
            task.launch()

        if task.event.wait(timeout=task.threshold * 0.8):
            if task.is_recording and was_recording:
                task.finish()
        else:
            if not was_recording:
                task.cancel()
