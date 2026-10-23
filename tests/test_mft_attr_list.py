"""FIX-3 / FIX-8 回归：属性列表跨记录 + 回退原因码一致（``$DATA`` 不在基础记录里）。

真盘规模（QA `p0_3c_precise.py analyze F/H`）：F: 86 个、H: 100 个文件的基础记录里
**没有** ``$DATA``（数据在 extension record 里）。修复前它们被解析成
``movable=True, size=0, extents=[]``，规划层当成"0 簇、无需搬动" → UI 报"已整理"
而真实碎片一个字节没动（假成功）。

本文件用一个**假卷**（内存字节流 + 打桩的 ReadFile）覆盖两条处置路径：

* 句柄通道能补齐 extents → ``source="handle"`` 且 extents 非空、可移动；
* 句柄通道也拿不到 → ``movable=False`` 且 reason 非空（可见，不静默）。

另外钉住 FIX-8：非 NTFS 卷的回退原因码必须与 ``scanner.build_scan_channel`` 一致。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.mft import FALLBACK_NOT_NTFS  # noqa: E402
from app.core.scanner import build_scan_channel  # noqa: E402
from app.win32 import mft_reader  # noqa: E402
from tests._mft_fixtures import (  # noqa: E402
    build_record,
    directory_record,
    make_volume,
    resident_attr,
)
from tests.test_mft_channel import _extent_map, _metadata_records, fake_channel  # noqa: E402

#: ``$OBJECT_ID``：一个与 ``$DATA`` 无关的常驻属性，用来构造"有属性列表但没有 $DATA"
ATTR_OBJECT_ID = 0x40


def _volume_with_attr_list_file() -> object:
    """造一个含 ``docs\\attr.bin``（基础记录无 ``$DATA``）的假卷。"""
    records = _metadata_records()
    records.append(directory_record(16, "docs", parent=5))
    records.append(
        build_record(
            34,
            name="attr.bin",
            parent=16,
            attribute_list=True,
            extra_data_attrs=[resident_attr(ATTR_OBJECT_ID, bytes(16))],
        )
    )
    return make_volume(records)


def test_scan_recovers_attribute_list_file_via_handle() -> None:
    """句柄通道能读到 extents → 补进条目并改成 ``source="handle"``。"""
    volume = _volume_with_attr_list_file()
    table = {"R:/docs/attr.bin": [(0, 700, 3), (3, 900, 2)]}
    with fake_channel(volume):
        outcome = mft_reader.scan_volume_mft(
            volume.info, sample_size=0, retrieve=_extent_map(table)
        )

    assert outcome.ok is True, outcome.notes
    entry = next(e for e in outcome.entries if e.name == "attr.bin")
    assert entry.extents, "句柄通道补齐后必须真的有 extents"
    assert [(e.vcn, e.lcn, e.clusters) for e in entry.extents] == [
        (0, 700, 3),
        (3, 900, 2),
    ]
    assert entry.source == "handle"
    assert entry.movable is True
    assert entry.incomplete is False
    assert any("属性列表降级" in note for note in outcome.notes)


def test_scan_keeps_attribute_list_file_when_handle_fails() -> None:
    """句柄通道也拿不到 → ``movable=False`` 且 reason 非空（必须被看见，不许沉默）。"""
    volume = _volume_with_attr_list_file()
    with fake_channel(volume):
        outcome = mft_reader.scan_volume_mft(
            volume.info, sample_size=0, retrieve=_extent_map({})   # 全部打不开
        )

    assert outcome.ok is True, outcome.notes
    entry = next(e for e in outcome.entries if e.name == "attr.bin")
    assert entry.movable is False
    assert entry.extents == []
    assert entry.reason, "不可移动必须给出原因"
    assert "属性列表" in entry.reason
    assert any("属性列表降级" in note for note in outcome.notes)


def test_scan_never_reports_movable_without_extents() -> None:
    """全局判据：扫描结果里任何 ``movable=True`` 的条目都必须有 size 与 extents。

    判据原文（主理人 / QA）：不允许出现 ``movable=True`` 且 ``extents`` 为空
    （不论 ``reason`` 空不空）—— 那种条目会被规划层当成"0 簇、已就位"。
    """
    records = _metadata_records()
    records.append(directory_record(16, "docs", parent=5))
    records.append(build_record(17, name="ok.bin", parent=16, runs=((300, 4),)))
    records.append(build_record(18, name="tiny.txt", parent=16, resident_data=b"hi"))
    records.append(
        build_record(
            19,
            name="attr.bin",
            parent=16,
            attribute_list=True,
            extra_data_attrs=[resident_attr(ATTR_OBJECT_ID, bytes(16))],
        )
    )
    volume = make_volume(records)
    table = {
        "R:/docs/ok.bin": [(0, 300, 4)],
        "R:/docs/attr.bin": [(0, 700, 2)],
    }
    with fake_channel(volume):
        outcome = mft_reader.scan_volume_mft(
            volume.info, sample_size=0, retrieve=_extent_map(table)
        )

    assert outcome.ok is True, outcome.notes
    bad = [
        entry
        for entry in outcome.entries
        if entry.movable and (not entry.extents or entry.size <= 0)
    ]
    assert bad == [], f"这些条目 movable=True 却没有 extents/size：{[(e.name, e.size) for e in bad]}"
    # 而且"空壳"确实被处置了：attr.bin 走的是句柄补齐
    attr = next(e for e in outcome.entries if e.name == "attr.bin")
    assert attr.movable is True and attr.extents


def test_non_ntfs_fallback_code_is_unified() -> None:
    """FIX-8：同一个 exFAT 卷，``scan_volume_mft`` 与 ``build_scan_channel`` 同码。"""
    volume = _volume_with_attr_list_file()
    volume.info.is_ntfs = False
    volume.info.filesystem = "exFAT"

    code_from_channel, _reason = build_scan_channel(volume.info, True)
    assert code_from_channel == "handle"

    with fake_channel(volume):
        outcome = mft_reader.scan_volume_mft(volume.info, sample_size=0)

    assert outcome.ok is False
    assert outcome.fallback_code == FALLBACK_NOT_NTFS
    assert outcome.channel == "handle"
    assert outcome.stats.fatal_error
    # 两处口径必须一致（UI 按码分支时才不会漏掉一半）
    assert _reason == FALLBACK_NOT_NTFS


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
