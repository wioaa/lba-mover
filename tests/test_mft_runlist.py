"""T02 验收 5 · runlist 解码（``app/core/mft.py::decode_runlist``）。

设计 §4.4 点名的三个坑各有用例：

1. ``off_size == 0`` → 稀疏段：``vcn`` 要推进、``lcn`` 记 -1；
2. ``delta`` 是**有符号**增量（``int.from_bytes(..., signed=True)``）；
3. ``cursor_lcn`` 是**跨 run 累加**的基准，不是每个 run 的绝对 LCN。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.mft import MAX_RUNS_PER_FILE, decode_runlist  # noqa: E402
from tests._mft_fixtures import encode_runlist  # noqa: E402


def _triples(extents) -> list:
    return [(e.vcn, e.lcn, e.clusters) for e in extents]


def test_runlist_single_run() -> None:
    """单 run：vcn 从 0 起，lcn 为绝对起点。"""
    extents, warnings = decode_runlist(encode_runlist([(100, 10)]))
    assert _triples(extents) == [(0, 100, 10)]
    assert warnings == []


def test_runlist_accumulates_positive_delta() -> None:
    """第二段 offset 为正：lcn = 上一段 lcn + delta。"""
    extents, warnings = decode_runlist(encode_runlist([(100, 10), (200, 5)]))
    assert _triples(extents) == [(0, 100, 10), (10, 200, 5)]
    assert warnings == []


def test_runlist_accumulates_negative_delta() -> None:
    """第二段 offset 为负（碎片回退）—— 最常见的实现错误点。"""
    extents, warnings = decode_runlist(encode_runlist([(500, 4), (100, 6)]))
    assert _triples(extents) == [(0, 500, 4), (4, 100, 6)]
    assert warnings == []


def test_runlist_sparse_advances_vcn_without_lcn() -> None:
    """稀疏段：lcn = -1，但 vcn 必须继续推进（否则整文件错位）。"""
    extents, warnings = decode_runlist(encode_runlist([(100, 10), (-1, 4), (200, 6)]))
    assert _triples(extents) == [(0, 100, 10), (10, -1, 4), (14, 200, 6)]
    assert warnings == []


def test_runlist_start_vcn_is_honoured() -> None:
    """分片 ``$DATA``：``start_vcn`` 参与累加，保证 vcn 全局连续。"""
    extents, _ = decode_runlist(encode_runlist([(300, 8)]), start_vcn=16)
    assert _triples(extents) == [(16, 300, 8)]


def test_runlist_zero_length_field_warns() -> None:
    """``len_size == 0`` → 警告且不崩溃（返回已解析部分）。"""
    first_run = bytes([0x42, 0x0A, 0x00]) + (100).to_bytes(4, "little", signed=True)
    bogus = bytes([0x40]) + b"\x00" * 4          # 声明 length_size=0
    extents, warnings = decode_runlist(first_run + bogus)
    assert _triples(extents) == [(0, 100, 10)]
    assert any("长度字段" in w for w in warnings)


def test_runlist_negative_lcn_warns_and_truncates() -> None:
    """累加后 LCN 为负 → 警告 + 返回已解析部分（不猜修正值）。"""
    blob = bytes([0x42, 0x10, 0x00]) + (-1000).to_bytes(4, "little", signed=True) + b"\x00"
    extents, warnings = decode_runlist(blob)
    assert extents == []
    assert any("为负" in w for w in warnings)


def test_runlist_truncated_warns() -> None:
    """截断的 runlist 必须给警告，不抛异常。"""
    blob = bytes([0x42, 0x10])  # 声明长度 2 字节 + 偏移 4 字节，实际只有长度
    extents, warnings = decode_runlist(blob)
    assert extents == []
    assert any("截断" in w for w in warnings)


def test_runlist_stops_at_max_runs() -> None:
    """run 数超上限即停止（防御被污染的 runlist 吃满内存）。"""
    runs = [(index * 2 + 10, 1) for index in range(MAX_RUNS_PER_FILE + 50)]
    extents, warnings = decode_runlist(encode_runlist(runs))
    assert len(extents) == MAX_RUNS_PER_FILE
    assert any("上限" in w for w in warnings)


def test_runlist_empty_is_valid() -> None:
    """空 runlist（只有结束标记）合法，返回空 + 无警告。"""
    extents, warnings = decode_runlist(b"\x00")
    assert extents == []
    assert warnings == []


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
