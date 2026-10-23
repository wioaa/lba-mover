"""T02 验收 3/4/6/7/8 · fixup、记录解析、路径重建、排除规则。

全部使用**构造的二进制夹具**（``tests/_mft_fixtures.py``），不依赖真实盘。
重点守护两条设计点：

* R16：``apply_fixup`` 必须在**任何属性解析之前**调用 —— 用"跨扇区边界的
  ``$FILE_NAME``"夹具专门验证（未还原的话名字必然乱码）；
* R17/验收 2：越界只会抛 ``MftBoundsError``，结构失败一律 ``None`` + 计数。
"""

from __future__ import annotations

import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.mft import (  # noqa: E402
    ATTR_DATA,
    MftRecord,
    MftScanStats,
    apply_fixup,
    parse_record,
    rebuild_path,
    record_to_entry,
)
from tests._mft_fixtures import (  # noqa: E402
    BYTES_PER_CLUSTER,
    RECORD_BYTES,
    build_record,
    directory_record,
    encode_runlist,
    non_resident_attr,
    resident_attr,
)


# --------------------------------------------------------------------------- #
# fixup
# --------------------------------------------------------------------------- #
def test_fixup_restores_name_spanning_sector_boundary() -> None:
    """★ 跨扇区的 ``$FILE_NAME``：fixup 没做好 → 名字乱码（R16 专项）。"""
    long_name = "跨扇区名字" * 30
    raw = build_record(17, name=long_name, runs=((100, 4),))
    record = parse_record(raw, 17, RECORD_BYTES)
    assert record is not None
    assert record.name == long_name


def test_fixup_rejects_torn_record() -> None:
    """某个扇区尾字被改成非法值 → 记录撕裂 → 返回 False（必须丢弃）。"""
    raw = bytearray(build_record(17))
    struct.pack_into("<H", raw, 1022, 0xBEEF)      # 第二个扇区的 USN 位置
    assert apply_fixup(raw, RECORD_BYTES) is False
    stats = MftScanStats()
    assert parse_record(bytes(raw), 17, RECORD_BYTES, stats) is None
    assert stats.bad_fixup == 1


def test_fixup_rejects_wrong_usa_count() -> None:
    """``usa_count`` 与实际扇区数不符 → False（设计验收 3③）。"""
    raw = bytearray(build_record(17))
    struct.pack_into("<H", raw, 0x06, 4)           # 1024/512+1 = 3，这里故意写 4
    assert apply_fixup(raw, RECORD_BYTES) is False


def test_fixup_rejects_usa_offset_overflow() -> None:
    """``usa_offset`` 越出记录 → False（设计验收 3④）。"""
    raw = bytearray(build_record(17))
    struct.pack_into("<H", raw, 0x04, 0xFFF0)
    assert apply_fixup(raw, RECORD_BYTES) is False


def test_fixup_rejects_short_record() -> None:
    """记录长度不足 → False（不抛）。"""
    assert apply_fixup(bytearray(64), RECORD_BYTES) is False


# --------------------------------------------------------------------------- #
# parse_record
# --------------------------------------------------------------------------- #
def test_parse_record_basic_fields() -> None:
    raw = build_record(42, name="movie.mkv", parent=7, runs=((100, 10), (200, 6)))
    record = parse_record(raw, 42, RECORD_BYTES)
    assert record is not None
    assert record.record_no == 42
    assert record.in_use is True
    assert record.is_directory is False
    assert record.name == "movie.mkv"
    assert record.parent_ref == 7
    assert [(e.vcn, e.lcn, e.clusters) for e in record.extents] == [
        (0, 100, 10),
        (10, 200, 6),
    ]
    assert record.allocated_clusters == 16
    assert record.data_size == 16 * BYTES_PER_CLUSTER
    assert record.incomplete is False


def test_parse_record_bad_signature_counts() -> None:
    stats = MftScanStats()
    raw = build_record(42, garbage_signature=True)
    assert parse_record(raw, 42, RECORD_BYTES, stats) is None
    assert stats.bad_magic == 1


def test_parse_record_zeroed_is_not_an_anomaly() -> None:
    """全零记录 = 已分配但未使用的 MFT 空间：算"未使用"，不算"损坏"。

    真实卷上这类记录可以占绝大多数（例如 46,592 条记录里 41,229 条未使用，
    其中一部分是整条全零）。若算成 bad_magic，健康度会被误判成"结构异常"。
    """
    stats = MftScanStats()
    record = parse_record(bytes(RECORD_BYTES), 7, RECORD_BYTES, stats)
    assert record is not None
    assert record.in_use is False
    assert stats.bad_magic == 0
    assert stats.anomalies == 0


def test_parse_record_short_buffer_counts() -> None:
    stats = MftScanStats()
    assert parse_record(b"FILE" + b"\x00" * 10, 0, RECORD_BYTES, stats) is None
    assert stats.bad_magic == 1


def test_parse_record_resident_data() -> None:
    """常驻 ``$DATA``：``extents == []``、``resident == True``（不是 0 簇文件）。"""
    raw = build_record(42, resident_data=b"hello world")
    record = parse_record(raw, 42, RECORD_BYTES)
    assert record is not None
    assert record.resident is True
    assert record.extents == []
    assert record.allocated_clusters == 0
    assert record.data_size == len(b"hello world")

    entry = record_to_entry(record, "R:\\tiny.txt", True)
    assert entry is not None
    assert entry.movable is False
    assert "常驻" in entry.reason
    assert entry.allocated_clusters == 0


def test_parse_record_compressed_encrypted_sparse_flags() -> None:
    for flag, checker in (
        (0x0001, "is_compressed"),
        (0x4000, "is_encrypted"),
        (0x8000, "is_sparse"),
    ):
        raw = build_record(42, runs=((100, 4),), data_flags=flag)
        record = parse_record(raw, 42, RECORD_BYTES)
        assert record is not None
        assert getattr(record, checker) is True


def test_parse_record_record_size_not_hardcoded() -> None:
    """同一逻辑文件在 1024 / 4096 记录大小下解析结果一致（验收 4）。"""
    raw_1k = build_record(99, name="same.bin", runs=((300, 8),), record_bytes=1024)
    raw_4k = build_record(99, name="same.bin", runs=((300, 8),), record_bytes=4096)
    a = parse_record(raw_1k, 99, 1024)
    b = parse_record(raw_4k, 99, 4096)
    assert a is not None and b is not None
    assert a.name == b.name == "same.bin"
    assert a.data_size == b.data_size
    assert [(e.vcn, e.lcn, e.clusters) for e in a.extents] == [
        (e.vcn, e.lcn, e.clusters) for e in b.extents
    ]


def test_parse_record_directory_has_no_data() -> None:
    raw = directory_record(20, "docs")
    record = parse_record(raw, 20, RECORD_BYTES)
    assert record is not None
    assert record.is_directory is True
    assert record.extents == []
    assert record_to_entry(record, "R:\\docs", True) is None


def test_parse_record_extension_record_marked() -> None:
    raw = build_record(60, name="part", base_ref=42)
    record = parse_record(raw, 60, RECORD_BYTES)
    assert record is not None
    assert record.base_record_ref == 42
    assert record_to_entry(record, "R:\\x", True) is None


def test_parse_record_attribute_list_incomplete() -> None:
    """属性列表存在但无法确认覆盖范围 → ``incomplete``（走句柄复核）。"""
    raw = build_record(42, runs=((100, 4),), attribute_list=True, data_size=10 * BYTES_PER_CLUSTER)
    record = parse_record(raw, 42, RECORD_BYTES, bytes_per_cluster=BYTES_PER_CLUSTER)
    assert record is not None
    assert record.has_attribute_list is True
    assert record.incomplete is True
    entry = record_to_entry(record, "R:\\big.bin", True)
    assert entry is not None and entry.movable is False
    assert "属性列表" in entry.reason


def test_parse_record_attribute_list_complete_is_accepted() -> None:
    """属性列表存在但 runlist 已覆盖 data_size → 正常接受（验收 9①）。"""
    raw = build_record(42, runs=((100, 4),), attribute_list=True, data_size=4 * BYTES_PER_CLUSTER)
    record = parse_record(raw, 42, RECORD_BYTES, bytes_per_cluster=BYTES_PER_CLUSTER)
    assert record is not None
    assert record.incomplete is False
    entry = record_to_entry(record, "R:\\ok.bin", True)
    assert entry is not None and entry.movable is True


def test_parse_record_attribute_list_without_data_is_excluded() -> None:
    """基础记录里没有 ``$DATA``（数据在扩展记录里）→ 必须排除，不许静默假成功。

    ★ FIX-3 的核心回归钉子。真盘上 F: 86 个 / H: 100 个文件是这个形态。
    修复前：``size == 0``、``covered == 0``，``size > covered`` 为 False →
    ``incomplete=False`` → 产出 ``movable=True, size=0, extents=[]``。
    规划层看到“0 簇、无需搬动”就判“已就位”，UI 报“已整理”，而真实碎片
    （例如 3 段共 32,823 簇 ≈ 128 MB）一个字节都没动。
    """
    raw = build_record(
        42,
        name="attr.bin",
        attribute_list=True,
        extra_data_attrs=[resident_attr(0x40, b"\x00" * 16)],   # $OBJECT_ID，故意不给 $DATA
    )
    record = parse_record(raw, 42, RECORD_BYTES, bytes_per_cluster=BYTES_PER_CLUSTER)
    assert record is not None
    assert record.has_attribute_list is True
    assert record.extents == []
    assert record.incomplete is True
    assert any("没有 $DATA" in warning for warning in record.warnings)

    entry = record_to_entry(record, "R:\attr.bin", True)
    assert entry is not None
    assert entry.movable is False, "空 extents 的条目绝不能被标成可移动（会被当成已就位）"
    assert entry.incomplete is True
    assert "属性列表" in entry.reason
    assert entry.extents == []


def test_parse_record_empty_runlist_never_movable() -> None:
    """非空文件但 runlist 为空（布局未知）→ 兜底排除，movable 必须为 False。

    这是“全局判据”的钉子：任何 ``movable=True`` 的条目都必须
    ``size > 0`` 且 ``extents`` 非空。
    """
    raw = build_record(43, name="empty.bin", runs=())
    record = parse_record(raw, 43, RECORD_BYTES, bytes_per_cluster=BYTES_PER_CLUSTER)
    assert record is not None
    assert record.allocated_clusters == 0
    assert record.incomplete is False          # runlist 本身没报错
    entry = record_to_entry(record, "R:\empty.bin", True)
    assert entry is not None
    assert entry.movable is False
    assert entry.reason, "排除原因必须写清楚（不许沉默）"
    assert "无有效 extents" in entry.reason


def test_parse_record_vcn_hole_marks_incomplete() -> None:
    """分片 ``$DATA`` 出现 vcn 空洞 → ``incomplete``（绝不猜）。"""
    first = non_resident_attr(
        ATTR_DATA, encode_runlist([(100, 4)]), 8 * BYTES_PER_CLUSTER, clusters=4
    )
    second = non_resident_attr(
        ATTR_DATA,
        encode_runlist([(200, 4)]),
        8 * BYTES_PER_CLUSTER,
        start_vcn=999,
        clusters=4,
    )
    raw = build_record(42, extra_data_attrs=[first, second])
    record = parse_record(raw, 42, RECORD_BYTES, bytes_per_cluster=BYTES_PER_CLUSTER)
    assert record is not None
    assert record.incomplete is True
    assert any("vcn" in w for w in record.warnings)


def test_parse_record_ads_is_counted_not_summed() -> None:
    """命名流（ADS）跳过且计数，不计入 ``allocated_clusters``（验收 9④）。"""
    stats = MftScanStats()
    raw = build_record(42, runs=((100, 4),), ads_name="Zone.Identifier")
    record = parse_record(raw, 42, RECORD_BYTES, stats)
    assert record is not None
    assert record.has_ads is True
    assert stats.ads_skipped == 1
    assert record.allocated_clusters == 4          # 只有主流
    entry = record_to_entry(record, "R:\\x.bin", True)
    assert entry is not None
    assert "ADS" in entry.reason


# --------------------------------------------------------------------------- #
# 排除规则（record_to_entry）
# --------------------------------------------------------------------------- #
def test_metadata_and_unused_records_produce_no_entry() -> None:
    """记录号 0–15 全部不产出 FileEntry（验收 7）。"""
    for number in range(16):
        raw = build_record(number, name=f"meta{number}")
        record = parse_record(raw, number, RECORD_BYTES)
        assert record is not None
        assert record_to_entry(record, "R:\\x", True) is None


def test_root_name_still_enters_path_table() -> None:
    """``$Root``(5) 不产出条目，但路径能从它重建（验收 7）。"""
    raw = build_record(5, name=".", parent=5)
    record = parse_record(raw, 5, RECORD_BYTES)
    assert record is not None
    assert record_to_entry(record, "R:\\", True) is None
    names = {5: (5, ".")}
    names[16] = (5, "docs")
    path, ok = rebuild_path(16, names)
    assert ok is True
    assert path == "docs"


def test_entry_reasons_for_special_files() -> None:
    cases = [
        (0x00000800, "压缩"),
        (0x00004000, "EFS"),
        (0x00040000, "重解析点"),
    ]
    for attributes, keyword in cases:
        raw = build_record(42, attributes=attributes, runs=((100, 4),))
        record = parse_record(raw, 42, RECORD_BYTES)
        assert record is not None
        entry = record_to_entry(record, "R:\\x", True)
        assert entry is not None
        assert entry.movable is False
        assert keyword in entry.reason

    raw = build_record(42, runs=((100, 4),), data_flags=0x8000)
    record = parse_record(raw, 42, RECORD_BYTES)
    assert record is not None
    entry = record_to_entry(record, "R:\\x", True)
    assert entry is not None and "稀疏" in entry.reason


def test_exclusive_system_file_is_shown_but_immovable() -> None:
    """D9：``pagefile.sys`` 必须**显示**（用户最想知道它占了哪块）但强标注不可移动。"""
    raw = build_record(42, name="pagefile.sys", runs=((100, 8),))
    record = parse_record(raw, 42, RECORD_BYTES)
    assert record is not None
    entry = record_to_entry(record, "R:\\pagefile.sys", True)
    assert entry is not None
    assert entry.movable is False
    assert "独占" in entry.reason
    assert entry.allocated_clusters == 8           # 位置信息仍然保留，供布局图显示


def test_path_unknown_is_immovable() -> None:
    raw = build_record(42, runs=((100, 4),))
    record = parse_record(raw, 42, RECORD_BYTES)
    assert record is not None
    entry = record_to_entry(record, "(record 42)", False)
    assert entry is not None
    assert entry.path_known is False
    assert entry.movable is False
    assert entry.source == "mft"
    assert entry.record_no == 42


# --------------------------------------------------------------------------- #
# 路径重建
# --------------------------------------------------------------------------- #
def test_rebuild_path_three_levels() -> None:
    names = {5: (5, "."), 20: (5, "docs"), 21: (20, "2024"), 22: (21, "a.txt")}
    path, ok = rebuild_path(22, names)
    assert ok is True
    assert path == "docs\\2024\\a.txt"


def test_rebuild_path_cycle_does_not_hang() -> None:
    """父链成环 → 返回占位符 + ``ok=False``，且必须**立刻**返回（验收 8②）。"""
    names = {5: (5, "."), 30: (31, "a"), 31: (30, "b")}
    started = time.perf_counter()
    path, ok = rebuild_path(30, names)
    elapsed = time.perf_counter() - started
    assert ok is False
    assert path == "(record 30)"
    assert elapsed < 1.0


def test_rebuild_path_depth_limit() -> None:
    """深度超限 → ``(record N)`` + False（验收 8③）。"""
    names = {}
    for number in range(100, 100 + 200):
        names[number] = (number + 1, f"d{number}")
    path, ok = rebuild_path(100, names, max_depth=64)
    assert ok is False
    assert path == "(record 100)"


def test_rebuild_path_missing_parent() -> None:
    """父记录缺失 → 占位符 + False（验收 8④）。"""
    path, ok = rebuild_path(77, {5: (5, ".")})
    assert ok is False
    assert path == "(record 77)"


def test_rebuild_path_metadata_name_fails() -> None:
    """链上出现元数据名字（``$`` 前缀）→ 不算有效路径。"""
    names = {5: (5, "."), 40: (5, "$Extend"), 41: (40, "x.bin")}
    path, ok = rebuild_path(41, names)
    assert ok is False
    assert path == "(record 41)"


def test_rebuild_path_self_reference() -> None:
    names = {5: (5, "."), 50: (50, "loop")}
    path, ok = rebuild_path(50, names)
    assert ok is False
    assert path == "(record 50)"


def test_root_record_itself_is_empty_path() -> None:
    path, ok = rebuild_path(5, {5: (5, ".")})
    assert ok is True
    assert path == ""


def test_mft_record_defaults_are_safe() -> None:
    """``MftRecord()`` 零参构造成立（其它模块做类型标注时不至于炸）。"""
    record = MftRecord()
    assert record.record_no == -1
    assert record.extents == []
    assert record.allocated_clusters == 0


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
