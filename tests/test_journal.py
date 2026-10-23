"""T02 验收：操作流水 ``app/core/journal.py``（设计文档 §6 T02 验收 4）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import journal as journal_mod  # noqa: E402
from app.core.journal import (  # noqa: E402
    RESULT_FAIL,
    RESULT_OK,
    JournalEntry,
    MoveJournal,
    default_journal_path,
    group_by_path,
)


def _tmp_path(name: str = "journal.jsonl") -> Path:
    folder = Path(tempfile.mkdtemp(prefix="lba-journal-"))
    return folder / name


# --------------------------------------------------------------------------- #
# 4) 写入可原样读回
# --------------------------------------------------------------------------- #
def test_append_and_load_roundtrip() -> None:
    path = _tmp_path()
    journal = MoveJournal(path)
    entries = [
        JournalEntry(phase="place", path="D:\\a.bin", vcn=0, src_lcn=10, dst_lcn=100,
                     clusters=5, result=RESULT_OK),
        JournalEntry(phase="place", path="D:\\a.bin", vcn=5, src_lcn=15, dst_lcn=105,
                     clusters=7, result=RESULT_OK),
        JournalEntry(phase="evict", path="D:\\b.bin", vcn=0, src_lcn=200, dst_lcn=900,
                     clusters=3, result=RESULT_FAIL, winerror=5, message="拒绝访问"),
    ]
    for entry in entries:
        journal.append(entry)
    journal.close()

    loaded = MoveJournal.load(path)
    assert len(loaded) == 3
    assert loaded[0].path == "D:\\a.bin" and loaded[0].clusters == 5
    assert loaded[2].result == RESULT_FAIL and loaded[2].winerror == 5
    assert loaded[2].message == "拒绝访问"
    # 每行都是合法 JSON（JSONL 契约）
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            json.loads(line)
    # ts 自动填充
    assert all(entry.ts for entry in loaded)


def test_append_is_immediately_readable() -> None:
    """逐段 flush：写入后不关闭也能立刻读到（崩溃可读性）。"""
    path = _tmp_path()
    journal = MoveJournal(path)
    journal.record(phase="place", path="X:\\a", vcn=0, src_lcn=1, dst_lcn=2, clusters=4, ok=True)
    assert len(MoveJournal.load(path)) == 1
    journal.record(phase="place", path="X:\\a", vcn=4, src_lcn=5, dst_lcn=6, clusters=4, ok=False,
                   winerror=87, message="参数错误")
    assert len(MoveJournal.load(path)) == 2
    journal.close()


def test_load_skips_broken_line() -> None:
    """断电写了半行时，load 必须跳过而不抛异常。"""
    path = _tmp_path()
    journal = MoveJournal(path)
    journal.record(phase="place", path="X:\\a", vcn=0, src_lcn=1, dst_lcn=2, clusters=4, ok=True)
    journal.close()
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"ts": "2026-01-01T00:00:00", "path": "X:\\\\b", "clu')  # 半行
    loaded = MoveJournal.load(path)
    assert len(loaded) == 1
    assert loaded[0].path == "X:\\a"


def test_load_missing_file() -> None:
    assert MoveJournal.load(_tmp_path("nope.jsonl")) == []


# --------------------------------------------------------------------------- #
# rollback_segments：逆序 + src↔dst 互换 + 只含成功段
# --------------------------------------------------------------------------- #
def test_rollback_segments_reversed_and_swapped() -> None:
    path = _tmp_path()
    journal = MoveJournal(path)
    journal.record(phase="place", path="D:\\a", vcn=0, src_lcn=10, dst_lcn=100, clusters=5, ok=True)
    journal.record(phase="place", path="D:\\a", vcn=5, src_lcn=15, dst_lcn=105, clusters=6, ok=True)
    journal.record(phase="evict", path="D:\\b", vcn=0, src_lcn=200, dst_lcn=300, clusters=7,
                   ok=False, winerror=32, message="共享冲突")
    journal.record(phase="place", path="D:\\c", vcn=0, src_lcn=400, dst_lcn=500, clusters=9, ok=True)
    journal.close()

    staged = journal.rollback_segments()
    assert len(staged) == 3                       # 失败段不参与
    paths = [p for p, _s in staged]
    assert paths == ["D:\\c", "D:\\a", "D:\\a"]   # 逆序
    first = staged[0][1]
    assert first.src_lcn == 500 and first.dst_lcn == 400      # src↔dst 互换
    assert first.vcn == 0 and first.clusters == 9
    second = staged[1][1]
    assert second.src_lcn == 105 and second.dst_lcn == 15


def test_rollback_clusters_and_stats() -> None:
    path = _tmp_path()
    journal = MoveJournal(path)
    journal.record(phase="place", path="A", vcn=0, src_lcn=1, dst_lcn=2, clusters=10, ok=True)
    journal.record(phase="place", path="A", vcn=10, src_lcn=11, dst_lcn=12, clusters=20, ok=True)
    journal.record(phase="place", path="B", vcn=0, src_lcn=1, dst_lcn=2, clusters=5, ok=False)
    journal.close()

    assert journal.moved_clusters == 30
    assert journal.moved_segments == 2
    assert journal.failed_segments == 1
    assert journal.rollback_clusters() == 30
    assert "成功 2 段" in journal.describe()


def test_empty_journal_rollback() -> None:
    journal = MoveJournal(_tmp_path("empty.jsonl"))
    assert journal.rollback_segments() == []
    assert journal.moved_clusters == 0
    journal.close()


def test_group_by_path_preserves_order() -> None:
    from app.core.models import MoveSegment

    items = [
        ("B", MoveSegment(0, 1, 2, 3)),
        ("A", MoveSegment(0, 4, 5, 6)),
        ("B", MoveSegment(3, 7, 8, 9)),
    ]
    grouped = group_by_path(items)
    assert [p for p, _ in grouped] == ["B", "A"]
    assert len(grouped[0][1]) == 2


def test_journal_context_manager_and_closed_guard() -> None:
    path = _tmp_path()
    with MoveJournal(path) as journal:
        journal.record(phase="place", path="A", vcn=0, src_lcn=1, dst_lcn=2, clusters=1, ok=True)
        assert journal.record_count == 1
    try:
        journal.append(JournalEntry())
    except RuntimeError:
        pass
    else:
        raise AssertionError("关闭后写入应当抛 RuntimeError")


def test_default_journal_path() -> None:
    path = default_journal_path("D")
    assert path.name.startswith("journal-D-")
    assert path.suffix == ".jsonl"
    assert path.parent.exists()
    assert "LBATailMover" in str(path)


def test_entry_json_roundtrip() -> None:
    entry = JournalEntry(
        ts="2026-01-01T00:00:00.000", phase="relay_in", path="中文\\路径.bin",
        vcn=7, src_lcn=8, dst_lcn=9, clusters=10, result=RESULT_OK, winerror=0, message=""
    )
    restored = JournalEntry.from_dict(json.loads(entry.to_json()))
    assert restored.path == entry.path
    assert restored.phase == "relay_in"
    assert restored.vcn == 7 and restored.clusters == 10
    # 多余字段不应导致崩溃
    extra = JournalEntry.from_dict({"path": "x", "unknown": 1})
    assert extra.path == "x"
    assert journal_mod.RESULT_OK == "ok"


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
