"""块设备层的契约。

``read_blocks`` 必须"要么给满，要么抛错"，这条契约是整个引擎正确性的
前提。所以在设备层就把它钉死，而不是指望上层每次都检查长度。
"""

from __future__ import annotations

import pytest

from lbamover.contract.enums import ErrorClass
from lbamover.contract.models import Geometry
from lbamover.devices import DeviceSpec, open_device
from lbamover.devices.base import DeviceError, check_range
from lbamover.devices.image import ImageDevice
from lbamover.devices.memory import MemoryDevice


class TestCheckRange:
    def test_ok(self):
        check_range(Geometry(4096, 100), 0, 100)
        check_range(Geometry(4096, 100), 99, 1)

    def test_overrun(self):
        with pytest.raises(DeviceError):
            check_range(Geometry(4096, 100), 95, 10)

    def test_negative(self):
        with pytest.raises(DeviceError):
            check_range(Geometry(4096, 100), -1, 1)
        with pytest.raises(DeviceError):
            check_range(Geometry(4096, 100), 0, -1)

    def test_zero_count_is_noop(self):
        check_range(Geometry(4096, 100), 500, 0)


class TestMemoryDevice:
    def test_geometry_and_defaults(self):
        d = MemoryDevice(64, 4096)
        assert d.geometry == Geometry(4096, 64)
        assert d.writable and not d.closed

    def test_read_write_roundtrip(self):
        d = MemoryDevice(16, 512)
        payload = bytes(range(256)) * 2
        d.write_blocks(3, payload)
        assert d.read_blocks(3, 1) == payload

    def test_read_counts_are_tracked(self):
        d = MemoryDevice(16, 512)
        d.write_blocks(0, b"\x01" * 512)
        assert d.write_calls == 1 and d.bytes_written == 512
        d.read_blocks(0, 2)
        assert d.read_calls == 1 and d.bytes_read == 1024

    def test_short_read_raises_instead_of_truncating(self):
        """核心契约：绝不返回短数据。"""
        d = MemoryDevice(16, 512)
        d.short_read_blocks = 2
        with pytest.raises(DeviceError) as exc:
            d.read_blocks(0, 4)
        assert exc.value.error_class is ErrorClass.RETRYABLE
        assert "短读" in str(exc.value)

    def test_write_length_must_be_block_multiple(self):
        d = MemoryDevice(16, 512)
        with pytest.raises(DeviceError):
            d.write_blocks(0, b"\x00" * 100)

    def test_out_of_range_write(self):
        d = MemoryDevice(4, 512)
        with pytest.raises(DeviceError):
            d.write_blocks(3, b"\x00" * 1024)

    def test_read_only_device_rejects_write(self):
        d = MemoryDevice(4, 512, writable=False)
        with pytest.raises(DeviceError):
            d.write_blocks(0, b"\x00" * 512)

    def test_closed_device_rejects_io(self):
        d = MemoryDevice(4, 512)
        d.close()
        assert d.closed
        with pytest.raises(DeviceError):
            d.read_blocks(0, 1)
        d.close()  # 幂等

    def test_context_manager(self):
        with MemoryDevice(4, 512) as d:
            d.write_blocks(0, b"\x02" * 512)
        assert d.closed

    def test_zero_count_read_returns_empty(self):
        assert MemoryDevice(4, 512).read_blocks(0, 0) == b""

    def test_fault_injection_counts_down(self):
        d = MemoryDevice(8, 512)
        d.fail_next_reads = 2
        for _ in range(2):
            with pytest.raises(DeviceError):
                d.read_blocks(0, 1)
        assert len(d.read_blocks(0, 1)) == 512  # 第三次成功

    def test_lba_fault_set(self):
        d = MemoryDevice(8, 512)
        d.fill_pattern(1)
        d.fail_lba_set = {5}
        with pytest.raises(DeviceError):
            d.write_blocks(4, b"\x00" * 1024)  # 覆盖到 LBA 5

    def test_fill_pattern_and_snapshot(self):
        d = MemoryDevice(8, 512)
        d.fill_pattern(3)
        snap = d.snapshot()
        assert len(snap) == 8 * 512
        assert d.nonzero_blocks() == 8

    def test_corrupt_writes_flag(self):
        d = MemoryDevice(4, 512)
        d.corrupt_writes = True
        d.write_blocks(0, b"\x10" * 512)
        assert d.read_blocks(0, 1) != b"\x10" * 512

    def test_invalid_initial_data(self):
        with pytest.raises(ValueError):
            MemoryDevice(2, 512, data=b"\x00" * 10)


class TestImageDevice:
    def test_create_and_roundtrip(self, tmp_path):
        path = tmp_path / "disk.img"
        dev = ImageDevice.create(str(path), block_count=32, block_size=512)
        payload = b"\xAB" * (512 * 4)
        dev.write_blocks(2, payload)
        dev.flush()
        assert dev.read_blocks(2, 4) == payload
        dev.close()

        again = ImageDevice(str(path), block_size=512, writable=False)
        assert again.read_blocks(2, 4) == payload
        assert not again.writable
        again.close()

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(DeviceError):
            ImageDevice(str(tmp_path / "nope.img"))

    def test_too_small_file_raises(self, tmp_path):
        p = tmp_path / "tiny.img"
        p.write_bytes(b"\x00" * 100)
        with pytest.raises(DeviceError):
            ImageDevice(str(p), block_size=512)

    def test_truncated_tail_reported(self, tmp_path):
        p = tmp_path / "odd.img"
        p.write_bytes(b"\x00" * (512 * 3 + 100))
        dev = ImageDevice(str(p), block_size=512)
        assert dev.geometry.block_count == 3
        assert dev.truncated_bytes == 100
        dev.close()

    def test_write_out_of_range(self, tmp_path):
        dev = ImageDevice.create(str(tmp_path / "d.img"), block_count=4, block_size=512)
        with pytest.raises(DeviceError):
            dev.write_blocks(4, b"\x00" * 512)
        dev.close()


class TestFactory:
    def test_memory(self):
        dev = open_device(DeviceSpec(kind="memory", block_count=8, block_size=512, writable=True))
        assert isinstance(dev, MemoryDevice) and dev.writable

    def test_image(self, tmp_path):
        p = tmp_path / "f.img"
        p.write_bytes(b"\x00" * 2048)
        dev = open_device(DeviceSpec(kind="image", path=str(p), block_size=512))
        assert isinstance(dev, ImageDevice)
        dev.close()

    def test_unknown_kind(self):
        with pytest.raises(DeviceError):
            open_device(DeviceSpec(kind="magic"))


class TestRetrievalTruncation:
    """缓冲被截断的判定。

    驱动在缓冲不足时**不报错**，只把填好的部分写回来。若就此把结果当成完整
    runlist，文件后半段的簇会被当成"空闲"—— 规划器随后会往那里写别人的数据。
    所以这一段判定必须单独测。
    """

    def _buf(self, extent_count: int, extents: int = 1) -> bytes:
        import struct

        # 结构是 DWORD ExtentCount + (4 字节对齐填充) + LARGE_INTEGER StartingVcn
        head = struct.pack("<I4xq", extent_count, 0)
        body = b"".join(struct.pack("<qq", i + 1, 100 + i) for i in range(extents))
        return head + body

    def test_exact_fit_is_not_truncated(self):
        from lbamover.devices.win32 import _retrieval_is_truncated

        raw = self._buf(1)
        assert _retrieval_is_truncated(raw, len(raw)) is False

    def test_short_buffer_is_truncated(self):
        from lbamover.devices.win32 import _retrieval_is_truncated

        raw = self._buf(4, extents=4)  # 声称 4 段
        assert _retrieval_is_truncated(raw, 16 + 2 * 16) is True

    def test_too_small_for_header_is_truncated(self):
        from lbamover.devices.win32 import _retrieval_is_truncated

        assert _retrieval_is_truncated(b"\x00" * 8, 8) is True

    def test_query_buffer_is_reused_per_thread(self):
        from lbamover.devices.win32 import IS_WINDOWS, _RETRIEVAL_INITIAL_BYTES, _query_buffer

        if not IS_WINDOWS:
            pytest.skip("Windows 专有路径")
        first = _query_buffer(_RETRIEVAL_INITIAL_BYTES)
        again = _query_buffer(16)
        assert first is again, "同一个线程应当复用同一块缓冲"
        bigger = _query_buffer(_RETRIEVAL_INITIAL_BYTES * 4)
        assert bigger is not first and bigger.size >= _RETRIEVAL_INITIAL_BYTES * 4
        assert _query_buffer(16) is bigger, "扩容后必须留着用"
