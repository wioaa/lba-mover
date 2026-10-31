"""断点：让「跑到 80% 断电」不再是灾难。

设计取舍说明清楚，因为这里最容易想当然：

* **落盘用「临时文件 + 原子替换」**。直接覆盖写一个 JSON，如果在写的中途
  掉电，留下的是半截文件，下次加载直接解析失败——断点等于没有。
* **断点带 ``plan_hash``**。换了一份计划却沿用旧断点，会把"在 A 位置写的
  数据"错当成"B 位置已完成"，这是能造成数据损坏的静默错误。hash 不一致
  时一律拒绝续做并要求重扫。
* **流水是 ``JSONL`` 追加**。追加写即使最后一行损坏，前面的记录仍然可读，
  而整体 JSON 数组会全部报废。事后追溯比"格式优雅"重要。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "Checkpoint",
    "CheckpointStore",
    "JsonlJournal",
    "default_state_dir",
]


def default_state_dir() -> Path:
    """断点/流水默认目录。放仓库内并 gitignore，避免污染用户的家目录。"""
    env = os.environ.get("LBAMOVER_STATE_DIR")
    if env:
        return Path(env)
    return Path.cwd() / ".lbamover"


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """一次任务的持久化状态。"""

    job_id: str
    plan_hash: str
    completed_ops: tuple[int, ...] = ()
    #: op_id → 已写入的块数（用于 op 内部续做）
    op_blocks: tuple[tuple[int, int], ...] = ()
    bytes_done: int = 0
    updated_at: float = field(default_factory=time.time)
    #: 自由格式备注，便于排查（例如最后一次失败原因）
    note: str = ""

    def blocks_of(self, op_id: int) -> int:
        for k, v in self.op_blocks:
            if k == op_id:
                return v
        return 0

    def is_op_done(self, op_id: int) -> bool:
        return op_id in self.completed_ops

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "job_id": self.job_id,
            "plan_hash": self.plan_hash,
            "completed_ops": list(self.completed_ops),
            "op_blocks": [[k, v] for k, v in self.op_blocks],
            "bytes_done": self.bytes_done,
            "updated_at": self.updated_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Checkpoint":
        return cls(
            job_id=str(raw.get("job_id", "")),
            plan_hash=str(raw.get("plan_hash", "")),
            completed_ops=tuple(int(x) for x in raw.get("completed_ops", ())),
            op_blocks=tuple((int(k), int(v)) for k, v in raw.get("op_blocks", ())),
            bytes_done=int(raw.get("bytes_done", 0)),
            updated_at=float(raw.get("updated_at", 0.0)),
            note=str(raw.get("note", "")),
        )


class CheckpointStore:
    """断点文件的读写。所有写操作原子且幂等。"""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def save(self, cp: Checkpoint) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(cp.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            # 失败时清理临时文件，绝不能留下垃圾（下次可能被误当断点）
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return self.path

    def load(self) -> Checkpoint | None:
        """读断点。任何损坏都返回 ``None``，并顺手把坏文件改名留证。"""
        if not self.path.is_file():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("断点内容不是对象")
            return Checkpoint.from_dict(raw)
        except (OSError, ValueError, TypeError):
            bad = self.path.with_suffix(self.path.suffix + ".bad")
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            return None

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


class JsonlJournal:
    """追加式执行流水。每行一条 JSON，天然可截断、可 tail。"""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._fh = None

    def append(self, record: dict) -> None:
        if self._fh is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")
        record = {**record, "ts": round(time.time(), 3)}
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def records(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # 尾部半行（断电）跳过，不影响前面的记录
        return out

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def __enter__(self) -> "JsonlJournal":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def merge_op_progress(
    existing: Iterable[tuple[int, int]], op_id: int, blocks: int
) -> tuple[tuple[int, int], ...]:
    """把某个 op 的已写块数更新进 ``op_blocks``（保留其它 op）。"""
    table = {k: v for k, v in existing}
    table[op_id] = max(table.get(op_id, 0), blocks)
    return tuple(sorted(table.items()))
