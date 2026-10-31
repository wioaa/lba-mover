"""NTFS 解析层：引导扇区、runlist、FILE 记录、文件表扫描。

这些用例全部跑在 ``ntfs.synth`` 造的**结构合法镜像**上，不碰真盘——
解析器的正确性必须能在没有管理员权限的机器上被反复验证。

重点盯两类错误：

* **偏移基准错了但结构仍合法**：runlist 的 delta 基准一旦写错，解出来的
  LCN 整体偏移，读到的还是"合法数据"，只是别人家的。
* **fixup 没还原**：每条记录每个扇区末尾 2 字节是假的，不还原就会在跨
  扇区时才出错，最难查。
"""

from __future__ import annotations

import pytest

from lbamover.contract.models import Extent
from lbamover.devices.memory import MemoryDevice
from lbamover.ntfs.boot import NtfsBoot, NotNtfsError
from lbamover.ntfs.catalog import MftCatalog, NtfsScanError
from lbamover.ntfs.record import NAMESPACE_WIN32_DOS, apply_fixups, parse_record
from lbamover.ntfs.runlist import RunlistError, decode_runs, encode_runs
from lbamover.ntfs.synth import build_ntfs_image


# ----------------------------------------------------------------------
# 引导扇区
# ----------------------------------------------------------------------


class TestBoot:
    def _device(self):
        dev = MemoryDevice(1024, 4096, writable=True, name="t")
        info = build_ntfs_image(dev, total_clusters=1024, record_slots=64)
        return dev, info

    def test_parse_real_bpb(self):
        dev, _ = self._device()
        boot = NtfsBoot.parse(dev.read_blocks(0, 1)[:512])
        assert boot.cluster_size == 4096
        assert boot.record_size == 1024
        assert boot.cluster_count == 1024
        assert boot.mft_lcn >= 1

    def test_rejects_non_ntfs(self):
        with pytest.raises(NotNtfsError):
            NtfsBoot.parse(b"\x00" * 512)

    def test_rejects_bad_cluster_count(self):
        dev, _ = self._device()
        raw = bytearray(dev.read_blocks(0, 1)[:512])
        raw[0x0D] = 0
        with pytest.raises(NotNtfsError):
            NtfsBoot.parse(bytes(raw))

    def test_rejects_short_sector(self):
        with pytest.raises(NotNtfsError):
            NtfsBoot.parse(b"NTFS    ")


# ----------------------------------------------------------------------
# runlist
# ----------------------------------------------------------------------


class TestRunlist:
    def test_single_run(self):
        rl = decode_runs(encode_runs([Extent(100, 5)]))
        assert rl.extents == (Extent(100, 5),)
        assert not rl.has_sparse
        assert rl.ok_for_move

    def test_delta_is_relative_to_previous_run_start(self):
        """基准错成"上一段末尾"时，第二段会整体偏移上一段的长度。"""
        extents = [Extent(120, 2), Extent(200, 3)]
        rl = decode_runs(encode_runs(extents))
        assert rl.extents == tuple(extents)

    def test_negative_delta(self):
        extents = [Extent(500, 4), Extent(200, 2)]
        assert decode_runs(encode_runs(extents)).extents == tuple(extents)

    def test_multi_run_roundtrip(self):
        extents = [Extent(10, 1), Extent(11, 2), Extent(1000, 7), Extent(20, 3)]
        assert decode_runs(encode_runs(extents)).extents == tuple(extents)

    def test_sparse_run_detected(self):
        # 头字节 0x01 = 长度 1 字节、偏移 0 字节 → 稀疏洞
        raw = bytes([0x01, 0x04]) + bytes([0x11, 0x02, 0x0A]) + bytes([0x00])
        rl = decode_runs(raw)
        assert rl.has_sparse
        assert not rl.ok_for_move
        assert rl.total_clusters == 6
        assert rl.extents == (Extent(10, 2),)

    def test_terminator_stops_decoding(self):
        raw = encode_runs([Extent(5, 1)]) + b"\xff\xff"
        assert decode_runs(raw).extents == (Extent(5, 1),)

    def test_truncated_runlist_raises(self):
        with pytest.raises(RunlistError):
            decode_runs(bytes([0x21, 0x04]))  # 声明 2 字节偏移但数据没了

    def test_bad_header_raises(self):
        with pytest.raises(RunlistError):
            decode_runs(bytes([0x0F, 0x01]))


# ----------------------------------------------------------------------
# FILE 记录
# ----------------------------------------------------------------------


class TestRecord:
    def _image(self):
        dev = MemoryDevice(1024, 4096, writable=True)
        info = build_ntfs_image(dev, total_clusters=1024, record_slots=64)
        return dev, info

    def test_fixups_restored(self):
        dev, info = self._image()
        raw = bytearray(dev.read_blocks(info.mft_lcn, 1)[:1024])
        # 直接解析必须先还原 fixup，否则扇区末尾 2 字节是 USN
        assert parse_record(bytes(raw), 0, 512) is not None
        # 破坏 USN 后应当判定为坏记录（返回 None），而不是解析出错数据
        broken = bytearray(raw)
        broken[510:512] = b"\xEE\xEE"
        assert parse_record(bytes(broken), 0, 512) is None

    def test_apply_fixups_rejects_mismatch(self):
        buf = bytearray(1024)
        buf[0:4] = b"FILE"
        buf[4:8] = (0x30).to_bytes(2, "little") + (3).to_bytes(2, "little")
        buf[0x30:0x32] = bytes([0xAA, 0xBB])  # USN
        buf[510:512] = bytes([0x01, 0x02])  # 与 USN 不一致 = 记录被撕裂
        assert apply_fixups(buf, 512) is False

    def test_resident_and_non_resident(self):
        dev, info = self._image()
        raw = dev.read_blocks(info.mft_lcn, 4)
        rec0 = parse_record(raw[:1024], 0, 512)
        assert rec0 is not None
        assert rec0.data is not None and rec0.data.non_resident
        assert rec0.extents == (Extent(info.mft_lcn, 16),)

    def test_name_namespace_prefers_win32(self):
        dev, info = self._image()
        raw = dev.read_blocks(info.mft_lcn, 8)
        rec = parse_record(raw[16 * 1024 : 17 * 1024], 16, 512)
        assert rec is not None
        assert rec.name == "alpha.bin"
        assert rec.best_name.namespace == NAMESPACE_WIN32_DOS
        assert rec.parent_ref == 5  # $Root

    def test_parse_garbage_returns_none(self):
        assert parse_record(b"\x00" * 1024, 0, 512) is None
        assert parse_record(b"MFT!" + b"\x00" * 1020, 0, 512) is None


# ----------------------------------------------------------------------
# 目录服务
# ----------------------------------------------------------------------


class TestCatalog:
    @pytest.fixture()
    def image(self):
        dev = MemoryDevice(1024, 4096, writable=True, name="ntfs.img")
        info = build_ntfs_image(dev, total_clusters=1024, record_slots=64)
        return dev, info

    def test_extents_match_written_layout(self, image):
        dev, info = image
        cat = MftCatalog.open(dev)
        stats = cat.scan()
        assert stats.records_bad == 0
        got = {f.path: f.extents for f in cat.list_files(None)}
        for name, extents in info.layout:
            assert got[name] == extents, f"{name} 解析结果与写入布局不一致"

    def test_metafiles_and_dirs_excluded(self, image):
        dev, _ = image
        cat = MftCatalog.open(dev)
        ids = {f.file_id for f in cat.list_files(None)}
        assert not (ids & {str(i) for i in range(16)}), "元文件不应出现在结果里"
        paths = {f.path for f in cat.list_files(None)}
        assert "data" not in paths, "目录不应出现在结果里"

    def test_nested_path_resolved(self, image):
        dev, _ = image
        cat = MftCatalog.open(dev)
        paths = {f.path for f in cat.list_files(None)}
        assert r"data\nested.bin" in paths

    def test_resident_small_file_skipped(self, image):
        dev, _ = image
        cat = MftCatalog.open(dev)
        paths = {f.path for f in cat.list_files(None)}
        assert "tiny.txt" not in paths, "常驻小文件没有独立簇，不该出现"

    def test_free_extents_from_bitmap(self, image):
        dev, info = image
        cat = MftCatalog.open(dev)
        stats = cat.scan()
        assert stats.free_source == "bitmap"
        free = cat.free_extents(None)
        assert free, "自由空间不应为空"
        # 自由簇数必须与镜像写入的位图完全对应
        assert sum(e.length for e in free) == info.cluster_count - info.used_clusters

    def test_scan_is_cached(self, image):
        dev, _ = image
        cat = MftCatalog.open(dev)
        first = cat.scan()
        second = cat.scan()
        assert first is second or first.elapsed_s == second.elapsed_s

    def test_non_ntfs_raises(self):
        dev = MemoryDevice(64, 4096, writable=True)
        with pytest.raises(NtfsScanError):
            MftCatalog.open(dev).scan()

    def test_truncated_mft_runs_does_not_crash(self, image):
        dev, _ = image
        cat = MftCatalog.open(dev, max_records=20)
        stats = cat.scan()
        assert stats.records_read <= 20


