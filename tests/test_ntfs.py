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

    def test_deleted_slot_returns_header_only(self):
        """已删除的记录槽只给头部。

        属性区是上一次使用的残留，随时会被复用覆盖，解析它没有意义 ——
        但**必须仍然把 in_use 如实报 False**，否则目录服务会把已删除文件
        当成活的搬走。
        """
        dev, info = self._image()
        raw = bytearray(dev.read_blocks(info.mft_lcn, 8)[16 * 1024 : 17 * 1024])
        raw[0x16] &= 0xFE  # 清掉 IN_USE 位（0x16 不在扇区边界，fixup 不受影响）
        rec = parse_record(bytes(raw), 16, 512)
        assert rec is not None
        assert rec.in_use is False
        assert rec.data is None and rec.names == ()
        assert rec.extents == ()


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




# ----------------------------------------------------------------------
# $Bitmap → 自由区间
# ----------------------------------------------------------------------


class TestBitmap:
    """位图解析的对拍：**以逐位朴素实现为基准**。

    这段代码被换掉过一次（从逐簇循环换成按字节跳跃），提速了几个数量级。
    提速本身没意义，正确才算数：自由区间错一位，规划器就会把别人的文件
    覆盖掉。所以这里随机造各种位图，与朴素实现逐段比对。
    """

    SIZES = [1, 2, 7, 8, 9, 63, 64, 65, 1000, 4096, 4097, 65535]

    def test_matches_naive_on_random_patterns(self):
        import random

        from lbamover.ntfs.bitmap import free_runs, free_runs_naive

        rng = random.Random(20261101)
        for trial in range(60):
            total = self.SIZES[trial % len(self.SIZES)]
            nb = (total + 7) // 8
            mode = trial % 4
            if mode == 0:
                bm = bytes(rng.getrandbits(8) for _ in range(nb))
            elif mode == 1:
                bm = b"\xff" * nb
            elif mode == 2:
                bm = b"\x00" * nb
            else:
                bm = bytes(rng.choice((0x00, 0xFF, 0x0F, 0xF0)) for _ in range(nb))
            assert free_runs(bm, total) == free_runs_naive(bm, total), (
                f"total={total} mode={mode}"
            )

    def test_short_bitmap_treats_missing_as_used(self):
        from lbamover.ntfs.bitmap import free_runs, free_runs_naive

        # 位图短于卷：缺失部分必须按"已用"算，不能猜成空闲
        for total, short in ((100, 3), (65536, 10), (9, 1)):
            bm = b"\x00" * short
            assert free_runs(bm, total) == free_runs_naive(bm, total)

    def test_run_spanning_internal_window(self):
        from lbamover.ntfs.bitmap import _WINDOW_BYTES, free_runs

        edge = _WINDOW_BYTES * 8  # 窗口边界对应的簇号
        total = edge * 2
        bm = bytearray(b"\xff" * ((total + 7) // 8))
        for c in range(edge - 6, edge + 8):
            bm[c >> 3] &= ~(1 << (c & 7)) & 0xFF
        assert free_runs(bytes(bm), total) == [(edge - 6, 14)]

    def test_zero_clusters(self):
        from lbamover.ntfs.bitmap import free_runs

        assert free_runs(b"", 0) == []
