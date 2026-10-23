"""T02 验收 1/2/9/10/11/12/13/14 · MFT 通道（``app/win32/mft_reader.py``）。

这里全部用**假卷**（内存字节流 + 打桩的 ``ReadFile``），不读真盘、不写任何东西。
最后一个用例是主理人点名要求的**实机抽样复核**（默认 32 个文件，按
``(vcn, lcn, clusters)`` 三元组比对），它只做读取。
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.mft import (  # noqa: E402
    FALLBACK_BOUNDS_VIOLATION,
    FALLBACK_NOT_NTFS,
    FALLBACK_NO_PRIVILEGE,
    FALLBACK_RUNLIST_UNAVAILABLE,
    FALLBACK_SAMPLE_MISMATCH,
    FALLBACK_STRUCTURE_INVALID,
    FALLBACK_USER_PREFERRED_HANDLE,
    MftBoundsError,
    MftScanStats,
    MftSpanGuard,
    MftStream,
    compare_extents,
    sample_pick,
)
from app.core.models import Extent, FileEntry, VolumeInfo  # noqa: E402
from app.win32 import api, mft_reader, retrieval  # noqa: E402
from tests._mft_fixtures import (  # noqa: E402
    BYTES_PER_CLUSTER,
    RECORD_BYTES,
    FakeVolume,
    build_record,
    directory_record,
    make_volume,
    patched_io,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = ROOT


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
@contextmanager
def fake_channel(volume: FakeVolume) -> Iterator[FakeVolume]:
    """把 ``open_mft_stream`` 换成假流 + 打桩全部原始读。"""
    original = mft_reader.open_mft_stream
    mft_reader.open_mft_stream = lambda letter, info: (volume.stream, [])  # type: ignore[assignment]
    try:
        with patched_io(volume) as patched:
            yield patched
    finally:
        mft_reader.open_mft_stream = original  # type: ignore[assignment]


def _extent_map(table: Dict[str, Sequence[Tuple[int, int, int]]]) -> Callable:
    """按路径返回预设 extents 的假"句柄通道"读取函数（路径分隔符不敏感）。"""
    normalized = {key.replace("\\", "/"): value for key, value in table.items()}

    def retrieve(path: str) -> Tuple[List[Extent], str]:
        runs = normalized.get(path.replace("\\", "/"))
        if runs is None:
            return [], "无法打开（假卷）"
        return [Extent(vcn=v, lcn=l, clusters=c) for v, l, c in runs], ""

    return retrieve


def _metadata_records() -> List[bytes]:
    """记录号 0–15 的模拟元数据记录（全部在用、不是目录的话会被当成文件）。"""
    records: List[bytes] = []
    for number in range(16):
        if number == 5:
            records.append(directory_record(5, "$Root", parent=5))
        else:
            records.append(build_record(number, name=f"$meta{number}", parent=5, runs=((20, 2),)))
    return records


# --------------------------------------------------------------------------- #
# 边界守卫（验收 1）
# --------------------------------------------------------------------------- #
def test_guard_rejects_six_illegal_cases() -> None:
    guard = MftSpanGuard(
        volume_bytes=1 << 20, mft_valid_bytes=3 * RECORD_BYTES, record_size=1024, sector_size=512
    )
    cases = [
        (0, 0, "长度 0"),
        (0, (1 << 20) + 1, "长度超 1 MiB"),
        (-512, 1024, "起点为负"),
        (1 << 20, 512, "越过卷尾"),
        (4 * RECORD_BYTES, RECORD_BYTES, "越过 MFT 有效范围"),
    ]
    for offset, length, label in cases:
        try:
            guard.check(offset, length)
        except MftBoundsError:
            continue
        raise AssertionError(f"{label} 未被拦下（offset={offset}, length={length}）")

    # ★ 第 6 类：卷尾不是扇区整数倍时，"为对齐多读的扇区"必须被算进校验
    odd = MftSpanGuard(
        volume_bytes=10_000, mft_valid_bytes=10_000, record_size=1024, sector_size=512
    )
    try:
        odd.check(9_900, 100)          # 原始区间 [9900, 10000) 合法
    except MftBoundsError:
        pass
    else:  # pragma: no cover
        raise AssertionError("对齐补读越界未被拦下（对齐后 [9728, 10752) 已越过卷尾）")


def test_guard_accepts_legal_boundary() -> None:
    guard = MftSpanGuard(
        volume_bytes=1 << 20, mft_valid_bytes=3 * RECORD_BYTES, record_size=1024, sector_size=512
    )
    guard.check(2 * RECORD_BYTES, RECORD_BYTES)      # 恰好在边界内
    guard.check(0, RECORD_BYTES)
    # 归一化：起点向下、终点向上对齐
    assert guard.normalize(1024, 1024) == (1024, 1024)
    assert guard.normalize(1000, 100) == (512, 1024)


# --------------------------------------------------------------------------- #
# 定位 $MFT（D6）
# --------------------------------------------------------------------------- #
def test_open_mft_stream_rejects_non_ntfs() -> None:
    stream, problems = mft_reader.open_mft_stream("Q", VolumeInfo(letter="Q", is_ntfs=False))
    assert stream.extents == []
    assert problems and "NTFS" in problems[0]


def test_open_mft_stream_needs_volume_info() -> None:
    info = VolumeInfo(letter="Q", is_ntfs=True, bytes_per_cluster=0, bytes_per_file_record=0)
    stream, problems = mft_reader.open_mft_stream("Q", info)
    assert stream.extents == []
    assert problems


def test_open_mft_stream_uses_retrieval_pointer_and_clips() -> None:
    """``CreateFile("X:\\$MFT")`` + ``FSCTL_GET_RETRIEVAL_POINTERS`` 的结果被正确采纳。"""
    info = VolumeInfo(
        letter="R",
        root="R:\\",
        is_ntfs=True,
        bytes_per_cluster=BYTES_PER_CLUSTER,
        bytes_per_sector=512,
        bytes_per_file_record=RECORD_BYTES,
        total_clusters=4096,
        mft_valid_data_length=20 * RECORD_BYTES,
    )
    original = retrieval.get_extents_for_path
    retrieval.get_extents_for_path = lambda path: ([Extent(0, 10, 8)], "")  # type: ignore[assignment]
    try:
        stream, problems = mft_reader.open_mft_stream("R", info)
    finally:
        retrieval.get_extents_for_path = original  # type: ignore[assignment]
    assert problems == []
    assert [(e.vcn, e.lcn, e.clusters) for e in stream.extents] == [(0, 10, 8)]
    assert stream.valid_length == 20 * RECORD_BYTES      # 被 MftValidDataLength 夹住
    assert stream.record_size == RECORD_BYTES


# --------------------------------------------------------------------------- #
# MftReader
# --------------------------------------------------------------------------- #
def test_reader_reads_across_fragmented_extents() -> None:
    """``$MFT`` 自身碎片化：跨 extent 读必须正确拼接（不能假设物理连续）。"""
    records = [build_record(index, name=f"f{index}.bin") for index in range(8)]
    volume = make_volume(records)
    volume.stream.extents = [Extent(vcn=0, lcn=10, clusters=1), Extent(vcn=1, lcn=50, clusters=1)]
    volume.stream.valid_length = 8 * RECORD_BYTES
    volume.disk[10 * BYTES_PER_CLUSTER : 11 * BYTES_PER_CLUSTER] = b"".join(records[:4])
    volume.disk[50 * BYTES_PER_CLUSTER : 51 * BYTES_PER_CLUSTER] = b"".join(records[4:])

    with fake_channel(volume):
        guard = mft_reader._build_guard(volume.info, volume.stream)
        reader = mft_reader.MftReader(0x5EED, volume.stream, guard)
        blob = reader.read(0, 8 * RECORD_BYTES)
    assert blob[:4] == b"FILE"
    assert blob[4 * RECORD_BYTES : 4 * RECORD_BYTES + 4] == b"FILE"
    assert blob == b"".join(records)


def test_iter_records_raises_beyond_valid_range() -> None:
    """★ 验收 2：``MftValidDataLength`` 只到第 3 条 → 第 4 条抛错（不是返回空）。"""
    records = [build_record(index, name=f"r{index}.bin") for index in range(3)]
    volume = make_volume(records)
    volume.stream.valid_length = 3 * RECORD_BYTES
    with fake_channel(volume):
        guard = mft_reader._build_guard(volume.info, volume.stream)
        reader = mft_reader.MftReader(0x5EED, volume.stream, guard)
        seen: List[int] = []
        try:
            for number, _raw in reader.iter_records(0, 4):
                seen.append(number)
        except MftBoundsError:
            pass
        else:  # pragma: no cover - 明确失败信息
            raise AssertionError(f"第 4 条记录未抛 MftBoundsError（已产出 {seen}）")
    assert seen == [0, 1, 2]


def test_scan_reports_bounds_violation_as_fallback() -> None:
    """越界 = 致命 = 回退（``read_error`` 必须为 0：说明是中止而非逐条吞）。"""
    records = [build_record(index, name=f"r{index}.bin") for index in range(4)]
    volume = make_volume(records, start_lcn=100, total_clusters=2)     # 卷只有 8 KiB
    volume.stream.valid_length = 4 * RECORD_BYTES
    with fake_channel(volume):
        outcome = mft_reader.scan_volume_mft(volume.info, sample_size=0)
    assert outcome.ok is False
    assert outcome.fallback_code == FALLBACK_BOUNDS_VIOLATION
    assert outcome.stats.fatal_error
    assert outcome.stats.read_error == 0


# --------------------------------------------------------------------------- #
# 抽样复核（验收 10）
# --------------------------------------------------------------------------- #
def test_compare_extents_uses_triples() -> None:
    base = [Extent(vcn=0, lcn=100, clusters=4), Extent(vcn=4, lcn=200, clusters=2)]
    assert compare_extents(base, list(base)) is True
    # ★ 仅 vcn 偏移 8：字节总数完全相同，但这是"文件中间段被搬走"的静默错乱
    shifted = [Extent(vcn=8, lcn=100, clusters=4), Extent(vcn=12, lcn=200, clusters=2)]
    assert compare_extents(base, shifted) is False
    # 一侧过滤掉**内部**稀疏段 → 不一致
    with_hole = [
        Extent(vcn=0, lcn=100, clusters=4),
        Extent(vcn=4, lcn=-1, clusters=2),
        Extent(vcn=6, lcn=200, clusters=2),
    ]
    assert compare_extents(with_hole, [with_hole[0], with_hole[2]]) is False
    # 单侧 lcn 不同 → 不一致
    assert compare_extents(base, [Extent(vcn=0, lcn=101, clusters=4)]) is False


def test_sample_pick_forces_extreme_cases() -> None:
    entries: List[FileEntry] = []
    for index in range(40):
        extents = [Extent(vcn=index, lcn=1000 + index, clusters=1)]
        entries.append(
            FileEntry(
                path=f"R:\\f{index}.bin",
                name=f"f{index}.bin",
                extents=extents,
                record_no=20 + index,
                source="mft",
            )
        )
    # 一个多碎片文件 + 一个最大文件 + 一个稀疏 + 一个含 ADS
    entries[3].extents = [Extent(vcn=i, lcn=5000 + i, clusters=1) for i in range(12)]
    entries[7].extents = [Extent(vcn=0, lcn=8000, clusters=900)]
    entries[9].extents = [Extent(vcn=0, lcn=9000, clusters=2), Extent(vcn=2, lcn=-1, clusters=5)]
    entries[9].attributes = 0x00000200
    entries[11].reason = "含 ADS，未计入"

    picks = sample_pick(entries, sample_size=8, rng_seed=7)
    picked = {e.name for e in picks}
    assert len(picks) == 8
    assert "f3.bin" in picked           # 最多碎片
    assert "f7.bin" in picked           # 体量最大
    assert "f9.bin" in picked           # 含稀疏
    assert "f11.bin" in picked          # 含 ADS
    # 候选不足时全量返回
    assert len(sample_pick(entries[:5], sample_size=32)) == 5


# --------------------------------------------------------------------------- #
# 全流程
# --------------------------------------------------------------------------- #
def _standard_volume() -> FakeVolume:
    records = _metadata_records()
    records.append(directory_record(16, "docs", parent=5))
    records.append(build_record(17, name="a.bin", parent=16, runs=((300, 4),)))
    records.append(build_record(18, name="b.bin", parent=16, runs=((400, 4), (500, 2))))
    records.append(build_record(19, name="c.bin", parent=5, runs=((600, 1),)))
    return make_volume(records)


def test_scan_full_flow_builds_entries_and_stats() -> None:
    volume = _standard_volume()
    table = {
        "R:\\docs\\a.bin": [(0, 300, 4)],
        "R:\\docs\\b.bin": [(0, 400, 4), (4, 500, 2)],
        "R:\\c.bin": [(0, 600, 1)],
    }
    with fake_channel(volume):
        outcome = mft_reader.scan_volume_mft(
            volume.info, sample_size=32, retrieve=_extent_map(table)
        )

    assert outcome.ok is True, outcome.notes
    assert outcome.channel == "mft"
    paths = sorted(entry.path for entry in outcome.entries)
    assert paths == ["R:\\c.bin", "R:\\docs\\a.bin", "R:\\docs\\b.bin"]
    by_path = {entry.path: entry for entry in outcome.entries}
    assert by_path["R:\\docs\\b.bin"].record_no == 18
    assert by_path["R:\\docs\\b.bin"].link_count == 1
    assert by_path["R:\\docs\\b.bin"].source == "mft"
    assert by_path["R:\\docs\\b.bin"].path_known is True

    stats = outcome.stats
    assert stats.record_size == RECORD_BYTES
    assert stats.skipped_metadata == 16
    assert stats.directory_records == 1
    assert stats.file_records == 3
    assert stats.sampled == 3
    assert stats.sample_mismatch == 0
    assert stats.anomalies == 0
    assert stats.healthy is True
    assert "抽样 3/3 一致" in stats.summary_line()

    # 归属索引（D2 的副产品）——同一批 entries 直接可建
    from app.core.ownership import OwnershipIndex

    index = OwnershipIndex.build(outcome.entries)
    assert index.extent_count == 4
    assert index.paths_in(300, 304) == ["R:\\docs\\a.bin"]


def test_scan_falls_back_when_structure_invalid() -> None:
    records = _metadata_records()
    records.append(directory_record(16, "docs", parent=5))
    for index in range(20, 32):
        records.append(build_record(index, name=f"g{index}.bin", garbage_signature=True))
    records.append(build_record(40, name="ok.bin", parent=16, runs=((300, 4),)))
    volume = make_volume(records)
    with fake_channel(volume):
        outcome = mft_reader.scan_volume_mft(volume.info, sample_size=0)
    assert outcome.ok is False
    assert outcome.fallback_code == FALLBACK_STRUCTURE_INVALID
    assert outcome.stats.bad_magic == 12
    assert outcome.stats.fatal_error


def test_scan_falls_back_when_sample_mismatch_exceeds_threshold() -> None:
    volume = _standard_volume()
    wrong = {
        "R:\\docs\\a.bin": [(0, 999, 4)],
        "R:\\docs\\b.bin": [(0, 998, 6)],
        "R:\\c.bin": [(0, 997, 1)],
    }
    with fake_channel(volume):
        outcome = mft_reader.scan_volume_mft(
            volume.info, sample_size=32, retrieve=_extent_map(wrong)
        )
    assert outcome.ok is False
    assert outcome.fallback_code == FALLBACK_SAMPLE_MISMATCH
    assert outcome.stats.sample_mismatch == outcome.stats.sampled == 3


def test_attribute_list_falls_back_to_handle_channel() -> None:
    """属性列表跨记录 → **文件级**回退句柄通道补齐（验收 9②）。"""
    records = _metadata_records()
    records.append(directory_record(16, "docs", parent=5))
    records.append(
        build_record(
            17,
            name="huge.bin",
            parent=16,
            runs=((300, 4),),
            attribute_list=True,
            data_size=100 * BYTES_PER_CLUSTER,
        )
    )
    records.append(
        build_record(
            18,
            name="hope.bin",
            parent=16,
            runs=((400, 4),),
            attribute_list=True,
            data_size=100 * BYTES_PER_CLUSTER,
        )
    )
    volume = make_volume(records)
    table = {
        "R:/docs/huge.bin": [(0, 300, 60), (60, 900, 40)],
        # hope.bin 故意读不到 → 必须保持"不可移动"，不许猜
    }
    with fake_channel(volume):
        outcome = mft_reader.scan_volume_mft(
            volume.info, sample_size=0, retrieve=_extent_map(table)
        )
    assert outcome.ok is True
    by_name = {entry.name: entry for entry in outcome.entries}
    assert by_name["huge.bin"].source == "handle"
    assert by_name["huge.bin"].movable is True
    assert [(e.vcn, e.lcn, e.clusters) for e in by_name["huge.bin"].extents] == [
        (0, 300, 60),
        (60, 900, 40),
    ]
    assert by_name["hope.bin"].movable is False
    assert "属性列表" in by_name["hope.bin"].reason
    assert any("补齐失败" in note for note in outcome.notes)


def test_scan_downgrades_only_mismatched_file() -> None:
    """不一致率 ≤5% → 文件级降级：只有那一个文件改走句柄结果（验收 10）。"""
    records = _metadata_records()
    records.append(directory_record(16, "docs", parent=5))
    for index in range(40):
        records.append(
            build_record(20 + index, name=f"f{index}.bin", parent=16, runs=((700 + index, 1),))
        )
    # 让 0 号成为"最多碎片"，必然被抽中；它的 MFT 结果与句柄结果故意不同
    records[16 + 1] = build_record(
        20, name="f0.bin", parent=16, runs=tuple((5000 + i, 1) for i in range(12))
    )
    volume = make_volume(records)

    table: Dict[str, Sequence[Tuple[int, int, int]]] = {}
    for index in range(40):
        if index == 0:
            table["R:\\docs\\f0.bin"] = [(i, 1000 + i, 1) for i in range(12)]   # 故意不同 lcn
        else:
            table[f"R:\\docs\\f{index}.bin"] = [(0, 700 + index, 1)]
    with fake_channel(volume):
        outcome = mft_reader.scan_volume_mft(
            volume.info, sample_size=32, retrieve=_extent_map(table)
        )
    assert outcome.ok is True
    assert 0 < outcome.stats.sample_mismatch <= 1
    entry = next(e for e in outcome.entries if e.name == "f0.bin")
    assert entry.source == "handle"
    assert [(e.vcn, e.lcn, e.clusters) for e in entry.extents] == [
        (i, 1000 + i, 1) for i in range(12)
    ]
    assert any("句柄通道复核" in note for note in outcome.notes)


def test_scan_counts_extension_ads_and_resident() -> None:
    records = _metadata_records()
    records.append(directory_record(16, "docs", parent=5))
    records.append(build_record(30, name="part", parent=16, runs=((300, 1),), base_ref=29))
    records.append(build_record(31, name="ads.bin", parent=16, runs=((310, 1),), ads_name="x"))
    records.append(build_record(32, name="tiny.txt", parent=16, resident_data=b"hi"))
    volume = make_volume(records)
    with fake_channel(volume):
        outcome = mft_reader.scan_volume_mft(volume.info, sample_size=0)
    assert outcome.ok is True
    assert outcome.stats.skipped_extension == 1
    assert outcome.stats.ads_skipped >= 1
    assert outcome.stats.resident_data == 1
    tiny = next(e for e in outcome.entries if e.name == "tiny.txt")
    assert tiny.movable is False and "常驻" in tiny.reason


def test_scan_marks_path_unknown_files() -> None:
    """父记录缺失 → 路径未知 → 不可移动（且不出现在可搬列表里）。"""
    records = _metadata_records()
    records.append(build_record(33, name="orphan.bin", parent=999, runs=((320, 1),)))
    volume = make_volume(records)
    with fake_channel(volume):
        outcome = mft_reader.scan_volume_mft(volume.info, sample_size=0)
    assert outcome.ok is True
    orphan = next(e for e in outcome.entries if e.name == "orphan.bin")
    assert orphan.path_known is False
    assert orphan.path == f"(record {orphan.record_no})"
    assert orphan.movable is False
    assert outcome.stats.path_unknown == 1


# --------------------------------------------------------------------------- #
# 只读与依赖隔离（验收 11）
# --------------------------------------------------------------------------- #
def test_reader_opens_volume_read_only() -> None:
    volume = _standard_volume()
    with fake_channel(volume):
        mft_reader.scan_volume_mft(volume.info, sample_size=0)
    assert volume.open_calls, "应当打开过一次卷设备"
    for path, access in volume.open_calls:
        assert access == api.GENERIC_READ, f"{path} 的访问权限不是只读：0x{access:X}"
        assert not access & api.GENERIC_WRITE
        assert "\\\\.\\" in path


def test_mft_chain_has_no_write_symbols() -> None:
    """grep 断言：MFT 链路零写符号（设计 §4.6.1）。"""
    for relative in ("app/core/mft.py", "app/win32/mft_reader.py", "app/core/ownership.py"):
        with open(os.path.join(PROJECT, relative), encoding="utf-8") as handle:
            text = handle.read()
        for symbol in (
            "GENERIC_WRITE",
            "FSCTL_MOVE_FILE",
            "WriteFile",
            "import mover",
            "app.win32.mover",
            "from app.win32 import mover",
        ):
            assert symbol not in text, f"{relative} 出现了写路径符号：{symbol}"


def test_mft_import_does_not_pull_mover() -> None:
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import app.core.mft, app.win32.mft_reader, app.win32.retrieval;"
        "print('app.win32.mover' in sys.modules)" % PROJECT
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert result.stdout.strip() == "False"


# --------------------------------------------------------------------------- #
# 通道决策与 worker 信号（验收 12）
# --------------------------------------------------------------------------- #
def test_build_scan_channel_reason_codes() -> None:
    from app.core import scanner

    good = VolumeInfo(
        letter="R",
        root="R:\\",
        is_ntfs=True,
        total_clusters=1000,
        bytes_per_cluster=4096,
        bytes_per_file_record=1024,
        mft_valid_data_length=1 << 20,
    )
    assert scanner.build_scan_channel(good, True) == ("mft", "")

    not_ntfs = VolumeInfo(letter="R", is_ntfs=False)
    assert scanner.build_scan_channel(not_ntfs, True)[1] == FALLBACK_NOT_NTFS

    no_privilege = VolumeInfo(
        letter="R",
        is_ntfs=True,
        total_clusters=0,
        bytes_per_cluster=4096,
        volume_error="[WinError 5] 打开卷设备失败：拒绝访问",
    )
    assert scanner.build_scan_channel(no_privilege, True)[1] == FALLBACK_NO_PRIVILEGE

    no_runlist = VolumeInfo(
        letter="R",
        is_ntfs=True,
        total_clusters=1000,
        bytes_per_cluster=4096,
        bytes_per_file_record=1024,
        mft_valid_data_length=0,
    )
    assert scanner.build_scan_channel(no_runlist, True)[1] == FALLBACK_RUNLIST_UNAVAILABLE

    assert scanner.build_scan_channel(good, False)[1] == FALLBACK_USER_PREFERRED_HANDLE
    for code in (
        FALLBACK_STRUCTURE_INVALID,
        FALLBACK_BOUNDS_VIOLATION,
        FALLBACK_SAMPLE_MISMATCH,
    ):
        assert scanner.build_scan_channel(good, True, code) == ("handle", code)


def test_mft_scan_worker_emits_fallback_not_failed() -> None:
    from PySide6.QtCore import QCoreApplication

    from app.core import scanner

    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None

    fallbacks: List[str] = []
    failures: List[str] = []
    worker = scanner.MftScanWorker(VolumeInfo(letter="R", is_ntfs=False))
    worker.fallback.connect(fallbacks.append)
    worker.failed.connect(failures.append)
    worker.run()
    assert fallbacks == [FALLBACK_NOT_NTFS]
    assert failures == []


def test_scan_worker_handle_mode_is_unchanged() -> None:
    """``ScanWorker(mode="handle")`` 与默认构造结果逐字段一致（R14 / 验收 13）。"""
    from PySide6.QtCore import QCoreApplication

    from app.core import scanner

    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None

    temp_dir = tempfile.mkdtemp(prefix="lba_scan_")
    try:
        for index in range(3):
            with open(os.path.join(temp_dir, f"f{index}.bin"), "wb") as handle:
                handle.write(b"x" * (8192 * (index + 1)))
        config = scanner.ScanConfig(root=temp_dir, min_size=1, max_matches=100)

        results: List[List[FileEntry]] = []

        def capture(entries: List[FileEntry]) -> None:
            results.append(entries)

        for worker in (scanner.ScanWorker(config), scanner.ScanWorker(config, mode="handle")):
            worker.completed.connect(capture)
            worker.run()

        assert len(results) == 2
        left, right = results
        assert [(e.path, e.size, e.movable, e.reason) for e in left] == [
            (e.path, e.size, e.movable, e.reason) for e in right
        ]
        assert [e.size for e in left] == sorted((e.size for e in left), reverse=True)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 本批最关键的证据：实机抽样复核（只读）
# --------------------------------------------------------------------------- #
def test_real_volume_mft_matches_retrieval_pointers() -> None:
    """真实卷：直读 MFT 算出的 extents 必须与 retrieval pointers **三元组**一致。

    覆盖要求（D7）：默认抽 32 个；候选不足则全量复核。要求至少核对 20 个文件，
    且一致率 100%。若本机没有可读的 NTFS 卷（非管理员），用例明确失败而不是
    静默通过。
    """
    from app.win32 import volume as volume_mod

    details: List[str] = []
    for info in volume_mod.list_volumes():
        if not info.is_ntfs or info.total_clusters <= 0 or info.mft_valid_data_length <= 0:
            continue
        outcome = mft_reader.scan_volume_mft(info, sample_size=32)
        if not outcome.ok:
            details.append(f"{info.letter}: 通道不可用（{outcome.fallback_code}）")
            continue

        picks = sample_pick(outcome.entries, sample_size=32)
        verified = 0
        matched = 0
        multi = 0
        mismatch_cases: List[str] = []
        for entry in picks:
            extents, reason = retrieval.get_extents_for_path(entry.path)
            if reason or not extents:
                continue
            verified += 1
            if len(entry.allocated_extents) > 1:
                multi += 1
            if compare_extents(entry.extents, extents):
                matched += 1
            else:
                mismatch_cases.append(
                    f"{entry.path}: MFT {[(e.vcn, e.lcn, e.clusters) for e in entry.extents]} "
                    f"vs 句柄 {[(e.vcn, e.lcn, e.clusters) for e in extents]}"
                )

        if verified < 20:
            details.append(f"{info.letter}: 实际核对仅 {verified} 个（样本不可达）")
            continue

        print(
            f"  [抽样复核] {info.letter}: 核对 {verified} 个文件，一致 {matched} 个"
            f"（其中多碎片 {multi} 个）；{outcome.stats.summary_line()}"
        )
        if mismatch_cases:
            for case in mismatch_cases[:8]:
                print(f"    不一致：{case}")
        assert matched == verified, (
            f"{info.letter} 抽样复核不一致 {len(mismatch_cases)}/{verified}：{mismatch_cases[:3]}"
        )
        return

    raise AssertionError(f"没有可用的 NTFS 卷完成抽样复核：{details[:5]}")


if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
