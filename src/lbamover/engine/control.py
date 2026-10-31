"""运行控制：取消、暂停、可中断等待。

为什么不用 ``threading.Event`` 直接暴露给调用方：暂停与取消的**语义**
容易搞混。这里把语义钉死：

* ``checkpoint()`` 在每个 chunk 边界调用。它**先查取消、再等暂停**。
  顺序不能反——反了的话，暂停状态下按停止会永远卡住。
* 取消是**一次性、不可撤销**的；暂停是可逆的。
* ``sleep()`` 可中断，避免退避等待期间取消需要等满 2 秒。
"""

from __future__ import annotations

import threading
import time

from .errors import CancelledError

__all__ = ["RunControl"]


class RunControl:
    """跨线程共享的控制块。所有方法都可从任意线程调用。"""

    __slots__ = ("_cancel", "_pause_lock", "_paused", "_resume_cv", "_pauses", "_cancels")

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._pause_lock = threading.Lock()
        self._paused = False
        self._resume_cv = threading.Condition(self._pause_lock)
        self._pauses = 0
        self._cancels = 0

    # -- 状态 -------------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def paused(self) -> bool:
        with self._pause_lock:
            return self._paused

    @property
    def pause_count(self) -> int:
        return self._pauses

    @property
    def cancel_count(self) -> int:
        return self._cancels

    # -- 动作 -------------------------------------------------------------

    def request_cancel(self) -> None:
        self._cancels += 1
        self._cancel.set()
        with self._resume_cv:
            # 先把暂停解开，否则等待暂停的线程看不到取消
            self._paused = False
            self._resume_cv.notify_all()

    def request_pause(self) -> None:
        with self._resume_cv:
            if not self._cancel.is_set():
                self._paused = True
                self._pauses += 1

    def request_resume(self) -> None:
        with self._resume_cv:
            self._paused = False
            self._resume_cv.notify_all()

    # -- 阻塞点 -----------------------------------------------------------

    def checkpoint(self) -> None:
        """chunk 边界调用。取消则抛 :class:`CancelledError`，暂停则原地等待。"""
        if self._cancel.is_set():
            raise CancelledError("任务已取消")
        with self._resume_cv:
            while self._paused and not self._cancel.is_set():
                self._resume_cv.wait(timeout=0.1)
            if self._cancel.is_set():
                raise CancelledError("任务已取消")

    def sleep(self, seconds: float) -> None:
        """可被取消中断的睡眠（退避等待用）。"""
        if seconds <= 0:
            self.checkpoint()
            return
        deadline = time.monotonic() + seconds
        while True:
            if self._cancel.is_set():
                raise CancelledError("退避等待期间被取消")
            left = deadline - time.monotonic()
            if left <= 0:
                return
            # 暂停期间也要能被取消唤醒
            with self._resume_cv:
                self._resume_cv.wait(timeout=min(left, 0.05))
