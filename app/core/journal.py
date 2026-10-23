"""操作流水（append-only JSONL）+ 逆序回滚段构造。

为什么需要它：``FSCTL_MOVE_FILE`` **没有内核级事务**，逐段提交。一旦中途崩溃，
必须能回答「哪些段真的搬了」。因此每段成功/失败都写一行并立即 ``flush()``
（OS 页缓存接住，进程崩溃不丢行）。

**fsync 批量化（1.0.0D 方案 A）**：``os.fsync`` 每行一次会把执行期变成
「搬一段 → 等一次刷盘」，HDD 上这是可测的吞吐损失。改为批量：攒满
:data:`JOURNAL_FSYNC_EVERY` 行（默认 32）或距上次 fsync 超过
:data:`JOURNAL_FSYNC_SECONDS` 秒（默认 2.0）才真正 ``os.fsync``。
**语义**：断电最多丢「页缓存里未 fsync 的尾部」，即最多 N 段日志记录
（默认 32）——**数据本身不受影响**（FSCTL 已完成），受影响的是回滚清单的
完整性。因此凡是要**启动回滚**、或 worker 结束/异常时，必须先
:meth:`MoveJournal.force_fsync` 把清单钉在盘上。

回滚不做自动执行（见设计 §3.7）：本模块只负责产出「逆序 + src↔dst 互换」的
段列表，由 UI 在用户确认后交给 mover 反向搬运。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from app.core.models import MoveSegment

RESULT_OK = "ok"
RESULT_FAIL = "fail"

#: 批量 fsync：攒满多少行才真正 ``os.fsync`` 一次（模块级常量，测试可覆盖）。
#: 断电窗口 = 未 fsync 的尾部行数；数据不受影响，影响的是回滚清单完整性。
JOURNAL_FSYNC_EVERY = 32

#: 批量 fsync 的时间上限（秒）：即使没攒满行数，超过该时长也刷一次，
#: 把「长时间低频写入」场景的断电窗口限制住。
JOURNAL_FSYNC_SECONDS = 2.0


@dataclass(slots=True)
class JournalEntry:
    """一条段级流水。"""

    ts: str = ""
    phase: str = "place"          # place | evict | relay_out | relay_in | rollback
    path: str = ""
    vcn: int = 0
    src_lcn: int = 0
    dst_lcn: int = 0
    clusters: int = 0
    result: str = RESULT_OK       # ok | fail
    winerror: int = 0
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.result == RESULT_OK

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict) -> "JournalEntry":
        known = {f.name for f in fields(cls)}
        payload = {k: v for k, v in data.items() if k in known}
        entry = cls()
        for key, value in payload.items():
            setattr(entry, key, value)
        return entry


def default_journal_path(letter: str = "") -> Path:
    """默认流水路径：``%LOCALAPPDATA%\\LBATailMover\\journal-<盘符>-<时间>.jsonl``。"""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    folder = Path(base) / "LBATailMover"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = (letter or "vol").upper().rstrip(":")
    return folder / f"journal-{tag}-{stamp}.jsonl"


class MoveJournal:
    """append-only JSONL 流水。

    用法::

        with MoveJournal(default_journal_path("D")) as journal:
            journal.append(JournalEntry(phase="place", path=..., result=RESULT_OK))
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._handle = None
        self._closed = False
        self._count = 0
        # ---- 1.0.0D 方案 A：批量 fsync 状态 ---- #
        self._since_fsync = 0
        self._last_fsync = time.monotonic()
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------- 写入 ---------------- #
    def _ensure_open(self):
        if self._handle is None or self._handle.closed:
            self._handle = self.path.open("a", encoding="utf-8", newline="\n")
        return self._handle

    def append(self, entry: JournalEntry) -> None:
        """追加一行并立即 ``flush()``；``os.fsync`` **批量**执行（1.0.0D 方案 A）。

        * 每行 ``write + flush``：OS 页缓存接住，**进程崩溃不丢行**；
        * ``fsync`` 攒批：``_since_fsync >= JOURNAL_FSYNC_EVERY`` 或距上次
          fsync ≥ ``JOURNAL_FSYNC_SECONDS`` 才真正刷盘；
        * **语义**：断电最多丢 N 段日志记录（默认 32）——数据本身不受影响
          （FSCTL 已完成），影响的是**回滚清单完整性**；凡要启动回滚前必须
          先调用 :meth:`force_fsync`。
        """
        if self._closed:
            raise RuntimeError("流水已关闭，无法继续写入")
        if not entry.ts:
            entry.ts = datetime.now().isoformat(timespec="milliseconds")
        handle = self._ensure_open()
        handle.write(entry.to_json() + "\n")
        handle.flush()
        self._since_fsync += 1
        if (
            self._since_fsync >= JOURNAL_FSYNC_EVERY
            or (time.monotonic() - self._last_fsync) >= JOURNAL_FSYNC_SECONDS
        ):
            self.force_fsync()
        self._count += 1

    def force_fsync(self) -> None:
        """无条件 ``os.fsync`` 并重置批量计数/计时。

        ★ **回滚前强制**：回滚清单的完整性是硬要求 —— 回滚流程启动前、执行
        worker 正常结束或异常失败时都必须调用，把「页缓存里还没落盘的尾部」
        钉到盘上，保证断电后 ``load()`` 能读到完整清单。
        """
        if self._handle is None or self._handle.closed:
            self._since_fsync = 0
            self._last_fsync = time.monotonic()
            return
        try:
            os.fsync(self._handle.fileno())
        except OSError:
            # 某些文件系统 / 设备不支持 fsync，flush 已经足够
            pass
        self._since_fsync = 0
        self._last_fsync = time.monotonic()

    def record(
        self,
        *,
        phase: str,
        path: str,
        vcn: int,
        src_lcn: int,
        dst_lcn: int,
        clusters: int,
        ok: bool,
        winerror: int = 0,
        message: str = "",
    ) -> JournalEntry:
        """便捷写法：直接按字段落一行。"""
        entry = JournalEntry(
            phase=phase,
            path=path,
            vcn=vcn,
            src_lcn=src_lcn,
            dst_lcn=dst_lcn,
            clusters=clusters,
            result=RESULT_OK if ok else RESULT_FAIL,
            winerror=winerror,
            message=message,
        )
        self.append(entry)
        return entry

    def close(self) -> None:
        """关闭文件句柄（幂等）；关闭前强制 fsync（清单完整性兜底）。"""
        if not self._closed:
            try:
                self.force_fsync()
            except Exception:  # noqa: BLE001 - 关闭路径绝不抛
                pass
        self._closed = True
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
        self._handle = None

    def __enter__(self) -> "MoveJournal":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---------------- 读取 ---------------- #
    @staticmethod
    def load(path: Path | str) -> List[JournalEntry]:
        """读回全部流水；坏行（例如断电写了半行）计数跳过，不抛异常。"""
        target = Path(path)
        entries: List[JournalEntry] = []
        if not target.exists():
            return entries
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    # 崩溃导致最后一行不完整 —— 跳过（属于已知的"最多丢一行"）
                    continue
                if not isinstance(data, dict):
                    continue
                entries.append(JournalEntry.from_dict(data))
        return entries

    @property
    def entries(self) -> List[JournalEntry]:
        return MoveJournal.load(self.path)

    # ---------------- 统计 / 回滚 ---------------- #
    @property
    def moved_clusters(self) -> int:
        return sum(e.clusters for e in self.entries if e.ok)

    @property
    def moved_segments(self) -> int:
        return sum(1 for e in self.entries if e.ok)

    @property
    def failed_segments(self) -> int:
        return sum(1 for e in self.entries if not e.ok)

    @property
    def record_count(self) -> int:
        """本次会话写入的行数。"""
        return self._count

    def rollback_segments(self) -> List[Tuple[str, MoveSegment]]:
        """构造回滚段：**逆序** + ``src ↔ dst`` 互换。

        只回滚成功的段（失败的段本来就没搬动）。同一文件可能有多段，逆序保证
        「后搬的先退」，避免中途撞占用。
        """
        staged: List[Tuple[str, MoveSegment]] = []
        for entry in reversed(self.entries):
            if not entry.ok or entry.clusters <= 0:
                continue
            staged.append(
                (
                    entry.path,
                    MoveSegment(
                        vcn=entry.vcn,
                        src_lcn=entry.dst_lcn,     # 现在在目标处
                        dst_lcn=entry.src_lcn,     # 搬回原处
                        clusters=entry.clusters,
                    ),
                )
            )
        return staged

    def rollback_clusters(self) -> int:
        return sum(seg.clusters for _path, seg in self.rollback_segments())

    def describe(self) -> str:
        return (
            f"流水 {self.path.name}：成功 {self.moved_segments} 段 / "
            f"{self.moved_clusters:,} 簇，失败 {self.failed_segments} 段"
        )


def group_by_path(
    segments: Sequence[Tuple[str, MoveSegment]],
) -> List[Tuple[str, List[MoveSegment]]]:
    """把 ``(path, segment)`` 列表按路径归组（**保持首次出现的顺序**）。"""
    order: List[str] = []
    buckets: dict = {}
    for path, segment in segments:
        if path not in buckets:
            buckets[path] = []
            order.append(path)
        buckets[path].append(segment)
    return [(path, buckets[path]) for path in order]
