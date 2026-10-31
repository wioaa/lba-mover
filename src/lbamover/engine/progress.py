"""进度与速率。

速率用**滑动窗口**而不是「总量 / 总耗时」。两者在长时间任务上差别很大：

* 总量/总耗时是"历史平均"，一旦中途暂停、重试、或换了介质，读数会长时间
  停在错误的量级上；
* 滑动窗口反映"此刻有多快"，用户看进度条是为了判断还要等多久，所以要的是
  当前速率。

ETA 用当前速率外推，速率过低（<1 KiB/s，通常是刚从暂停恢复）时返回
``None`` 而不是一个荒谬的大数——UI 显示"—"比显示"剩余 3 天"更诚实。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Deque

from ..contract.models import Progress

__all__ = ["RateMeter", "ProgressTracker"]


class RateMeter:
    """滑动窗口吞吐计量。线程安全由调用方保证（引擎在单线程里累加）。"""

    __slots__ = ("_window", "_samples", "_peak")

    def __init__(self, window_s: float = 2.0) -> None:
        if window_s <= 0:
            raise ValueError("window_s 必须为正")
        self._window = window_s
        self._samples: Deque[tuple[float, int]] = deque()
        self._peak = 0.0

    def clear(self) -> None:
        self._samples.clear()

    def add(self, n_bytes: int, now: float | None = None) -> None:
        if n_bytes <= 0:
            return
        t = time.monotonic() if now is None else now
        self._samples.append((t, n_bytes))
        self._trim(t)

    def _trim(self, now: float) -> None:
        cutoff = now - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def rate(self, now: float | None = None) -> float:
        """近窗口内的字节/秒。

        少于两个样本时返回 0：单个样本只能算出"瞬时速度"，跨度为微秒，
        除出来是个天文数字，ETA 会变成 0 秒。宁可不给读数，也不给假读数。
        """
        if len(self._samples) < 2:
            return 0.0
        t = time.monotonic() if now is None else now
        self._trim(t)
        if len(self._samples) < 2:
            return 0.0
        total = sum(n for _, n in self._samples)
        span = t - self._samples[0][0]
        if span <= 0:
            span = 1e-3
        rate = total / span
        self._peak = max(self._peak, rate)
        return rate

    @property
    def peak(self) -> float:
        return self._peak


class ProgressTracker:
    """累计进度 + 速率 + ETA。每次 ``snapshot()`` 产出一个不可变快照。"""

    __slots__ = (
        "block_size",
        "total_blocks",
        "done_blocks",
        "done_ops",
        "total_ops",
        "retried_chunks",
        "failed_chunks",
        "skipped_ops",
        "current_path",
        "_meter",
        "_started",
        "_skipped_blocks",
    )

    def __init__(self, total_blocks: int, block_size: int, total_ops: int = 0) -> None:
        self.block_size = block_size
        self.total_blocks = max(0, total_blocks)
        self.total_ops = max(0, total_ops)
        self.done_blocks = 0
        self.done_ops = 0
        self.retried_chunks = 0
        self.failed_chunks = 0
        self.skipped_ops = 0
        self.current_path = ""
        self._meter = RateMeter()
        self._started = time.monotonic()
        self._skipped_blocks = 0

    # -- 累加 -------------------------------------------------------------

    def advance(self, blocks: int, now: float | None = None) -> None:
        if blocks <= 0:
            return
        self.done_blocks += blocks
        self._meter.add(blocks * self.block_size, now)

    def complete_op(self) -> None:
        self.done_ops += 1

    def skip_op(self) -> None:
        self.skipped_ops += 1

    def note_retry(self) -> None:
        self.retried_chunks += 1

    def note_failure(self) -> None:
        self.failed_chunks += 1

    # -- 快照 -------------------------------------------------------------

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def snapshot(self, now: float | None = None) -> Progress:
        rate = self._meter.rate(now)
        remaining_blocks = max(0, self.total_blocks - self.done_blocks)
        eta: float | None = None
        if rate > 1024 and remaining_blocks > 0:
            eta = remaining_blocks * self.block_size / rate
        return Progress(
            done_blocks=self.done_blocks,
            total_blocks=self.total_blocks,
            done_ops=self.done_ops,
            total_ops=self.total_ops,
            bytes_done=self.done_blocks * self.block_size,
            bytes_total=self.total_blocks * self.block_size,
            rate_bps=rate,
            eta_s=eta,
            retried_chunks=self.retried_chunks,
            failed_chunks=self.failed_chunks,
            skipped_ops=self.skipped_ops,
            current_path=self.current_path,
            elapsed_s=self.elapsed,
        )
