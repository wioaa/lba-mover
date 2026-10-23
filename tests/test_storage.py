"""T01 验收 7 · 介质探测与卷磁盘范围（``app/win32/storage.py``，**只读**）。

这些用例打真实机器（只调用查询类 IOCTL，不写任何数据）：

* 非管理员下必须**不抛异常**，退回 ``unknown``；
* 只要能打开卷设备，``volume_disk_extents`` 就必须给出 ``starting_offset ≥ 0``；
* 失败时返回空结果而不是异常（调用方据此降级，而不是崩溃）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.models import MEDIA_HDD, MEDIA_SSD, MEDIA_UNKNOWN  # noqa: E402
from app.win32 import storage, volume  # noqa: E402


def test_probe_media_never_raises() -> None:
    """``probe_media`` 对任何盘符都必须返回对象而不是抛异常。"""
    for letter in ("C", "R", "Q", ""):
        probe = storage.probe_media(letter)
        assert probe.media in (MEDIA_HDD, MEDIA_SSD, MEDIA_UNKNOWN)
        assert probe.source in ("seek_penalty", "bus_type", "fallback")
        if probe.media == MEDIA_UNKNOWN:
            assert probe.note, "退回 unknown 时必须说明原因（不许沉默）"


def test_volume_disk_extents_readable_volume() -> None:
    """能打开卷设备的卷至少给出一条 extent，且起点非负。"""
    readable = [info for info in volume.list_volumes() if info.total_clusters > 0]
    if not readable:
        raise AssertionError("本机没有可读的 NTFS 卷，无法验证 volume_disk_extents")

    checked = 0
    for info in readable:
        mapping = storage.volume_disk_extents(info.letter)
        if not mapping.extents:
            continue
        checked += 1
        for extent in mapping.extents:
            assert extent.starting_offset >= 0
            assert extent.extent_length > 0
            assert extent.disk_number >= 0
        assert mapping.first_offset == min(
            extent.starting_offset for extent in mapping.extents
        )
    assert checked >= 1, "没有任何可读卷返回 extent"


def test_probe_volume_storage_combines_both() -> None:
    """``probe_volume_storage`` 一次性给出介质 + 磁盘范围（UI 安全论证用）。"""
    infos = volume.list_volumes()
    if not infos:
        raise AssertionError("本机没有可枚举的卷")
    probe, mapping = storage.probe_volume_storage(infos[0].letter)
    assert probe.media in (MEDIA_HDD, MEDIA_SSD, MEDIA_UNKNOWN)
    assert isinstance(mapping.extents, list)
    # 空结果也必须是"安静的空"，而不是异常
    assert isinstance(mapping.error, str)


# --------------------------------------------------------------------------- #
# SeekPenalty 常量与描述符解码（P0-1 回归钉子）
# --------------------------------------------------------------------------- #
def test_seek_penalty_property_id_is_sdk_value() -> None:
    """``StorageDeviceSeekPenaltyProperty`` 必须等于 SDK 的 7。

    v2 第一批曾误写成 4（``StorageDeviceWriteCacheProperty``），后果是读到写缓存
    描述符、把第 4 字节当 ``IncursSeekPenalty``，介质判定彻底失效 → HDD 永远
    拿不到加速。这个常量只要被人"顺手改回 4"，本用例必须立刻红。

    SDK 枚举顺序：0 Device / 1 Adapter / 2 DeviceId / 3 DeviceUniqueId /
    4 WriteCache / 5 Miniport / 6 AccessAlignment / **7 SeekPenalty** /
    8 Trim / 9 WriteAggregation。
    """
    assert storage.StorageDeviceSeekPenaltyProperty == 7
    assert storage.StorageDeviceSeekPenaltyProperty != 4  # WriteCache 不是它
    assert storage.StorageDeviceSeekPenaltyProperty != 6  # AccessAlignment 不是它


def test_decode_seek_penalty_header_layout() -> None:
    """12 字节带头布局 ``{Version, Size, IncursSeekPenalty}``：布尔在 offset 8。

    这是本机实测形态（``Version`` 字段被驱动填成 12，因此**不能**用
    ``Version == 1`` 做前置校验）。
    """
    hdd = bytes([12, 0, 0, 0, 12, 0, 0, 0, 1, 0, 0, 0])
    ssd = bytes([12, 0, 0, 0, 12, 0, 0, 0, 0, 0, 0, 0])
    assert storage.decode_seek_penalty(hdd) is True
    assert storage.decode_seek_penalty(ssd) is False
    # offset 4 是 Size（此处为 12），**不是**布尔：绝不能把它当标志位
    assert storage.decode_seek_penalty(ssd) is not True


def test_decode_seek_penalty_compact_layout() -> None:
    """8 字节紧凑布局 ``{Version, IncursSeekPenalty}``：布尔在 offset 4。"""
    hdd = bytes([1, 0, 0, 0, 1, 0, 0, 0])
    ssd = bytes([1, 0, 0, 0, 0, 0, 0, 0])
    assert storage.decode_seek_penalty(hdd) is True
    assert storage.decode_seek_penalty(ssd) is False


def test_decode_seek_penalty_rejects_garbage() -> None:
    """布局不明时必须返回 ``None``（继续降级），**不许猜**。"""
    assert storage.decode_seek_penalty(b"") is None
    assert storage.decode_seek_penalty(bytes([12, 0, 0, 0])) is None
    # 布尔位不是 0/1 → 说明布局判错，放弃
    bad = bytes([12, 0, 0, 0, 12, 0, 0, 0, 0x7F, 0, 0, 0])
    assert storage.decode_seek_penalty(bad) is None
    # 全零（驱动没填）：既不是 HDD 也不是"确定是 SSD"，必须退回保守模式
    assert storage.decode_seek_penalty(bytes(12)) is None
    # len=12 但 Size 字段被写成 0（驱动没填 Size）→ 同样不可信
    assert storage.decode_seek_penalty(bytes([12, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])) is None


def test_decode_seek_penalty_rejects_other_descriptors() -> None:
    """其它属性的描述符（28 / 24 字节）**绝不能**被当成 seek-penalty 解出来。

    这条是 P0-1 复验第 3 轮的核心回归钉子：早期实现用 ``size >= 12`` 无上界，
    28 字节的 ``STORAGE_ACCESS_ALIGNMENT_DESCRIPTOR``（prop=6）、写缓存描述符
    （prop=4）、未知 24 字节结构都会落到 offset 8 读到 0，被判成"确定的 SSD"。
    常量一旦被改回 4/6，或长度上界一旦被删掉，本用例必须立刻红。

    下面是**本机实测**的原始字节（只读查询，未写任何数据）。
    """
    # prop=4 写缓存描述符（28 字节）：offset4 = 0x1c = 28
    write_cache = bytes.fromhex(
        "1c000000" "1c000000" "02020201" "01000000" "00000000" "00000000" "00000000"
    )
    assert len(write_cache) == 28
    assert storage.decode_seek_penalty(write_cache) is None
    # 写缓存描述符但 IsWriteCacheEnabled = 0（关掉写缓存）→ 仍不是 SSD
    write_cache_off = bytearray(write_cache)
    write_cache_off[8] = 0
    assert storage.decode_seek_penalty(bytes(write_cache_off)) is None
    # prop=6 STORAGE_ACCESS_ALIGNMENT_DESCRIPTOR（28 字节）：
    # dwords = [28, 28, 0, 0, 512(offset12 逻辑扇区), 4096(offset16 物理扇区), 0]
    alignment = bytes.fromhex(
        "1c000000" "1c000000" "00000000" "00020000" "00100000" "00000000" "00000000"
    )
    assert len(alignment) == 28
    # 结构自证：offset12 = 512 逻辑扇区、offset16 = 4096 物理扇区
    import struct as _struct

    assert _struct.unpack_from("<I", alignment, 12)[0] == 512
    assert _struct.unpack_from("<I", alignment, 16)[0] == 4096
    # ★ 关键断言：offset8 恰好是 0，早期实现会把它当成"无寻道惩罚"→ 判 SSD
    assert alignment[8] == 0
    assert storage.decode_seek_penalty(alignment) is None
    # 未知 24 字节描述符（全零）
    assert storage.decode_seek_penalty(bytes(24)) is None
    # 未知 64 字节缓冲（驱动没按结构填）
    assert storage.decode_seek_penalty(bytes(64)) is None
    # 长度 12 但 Size 字段写成了 28（长度与 Size 自相矛盾）→ 不可信
    inconsistent = bytes([12, 0, 0, 0, 0x1C, 0, 0, 0, 0, 0, 0, 0])
    assert storage.decode_seek_penalty(inconsistent) is None


def test_volume_device_openable_without_elevation() -> None:
    """``open_volume_device`` 对系统卷必须能拿到句柄（零访问权限回退）。

    非提权时 ``GENERIC_READ`` 会被拒（错误码 5），若不回退到 ``access=0``，
    固定磁盘的介质永远是「未知」，HDD 加速功能等于不存在。
    """
    infos = volume.list_volumes()
    if not infos:
        raise AssertionError("本机没有可枚举的卷")
    for info in infos[:3]:
        try:
            handle = storage.open_volume_device(info.letter)
        except Exception as exc:  # noqa: BLE001 - 打不开要报出具体原因
            raise AssertionError(
                f"{info.letter}: 零访问权限也打不开卷设备：{exc}"
            ) from exc
        try:
            assert handle
        finally:
            storage.api.close_handle(handle)


def test_seek_penalty_source_is_honest() -> None:
    """凡声称 ``source == "seek_penalty"`` 的卷，必须给出确定的 HDD/SSD。

    并且要与**独立的裸 IOCTL 复算**逐一比对（P0-1 复验第 3 轮）：

    * 直接发 ``prop=7`` 查询，按"12 字节 → offset 8 / 8 字节 → offset 4"复算，
      与被测实现的解码结果比对 —— 只验"枚举合法"是拦不住偏移写错的；
    * 同时钉住"返回长度只能是 8 或 12"，否则说明该驱动返回的是别的描述符，
      此时被测实现**必须**给出 ``unknown``（保守模式）而不是 SSD。
    """
    infos = volume.list_volumes()
    if not infos:
        raise AssertionError("本机没有可枚举的卷")
    probed = 0
    for info in infos:
        probe = storage.probe_media(info.letter)
        if probe.source != "seek_penalty":
            continue
        probed += 1
        assert probe.media in (MEDIA_HDD, MEDIA_SSD)
        assert probe.incurs_seek_penalty is not None
        assert probe.media == (MEDIA_HDD if probe.incurs_seek_penalty else MEDIA_SSD)
        assert probe.describe()

        # ---- 独立复算（不复用 storage.decode_seek_penalty）----
        handle = storage.open_volume_device(info.letter, context="介质交叉验证")
        try:
            ok, raw, returned, _error = storage.api.device_io_control_raw(
                handle,
                storage.api.IOCTL_STORAGE_QUERY_PROPERTY,
                in_bytes=storage.struct.pack(
                    "<III", storage.StorageDeviceSeekPenaltyProperty, 0, 0
                ),
                out_size=64,
                context="SeekPenalty 交叉验证",
            )
        finally:
            storage.api.close_handle(handle)
        assert ok, f"{info.letter}: prop=7 裸查询失败"
        assert returned == len(raw)
        assert len(raw) in (8, 12), (
            f"{info.letter}: seek-penalty 描述符长度为 {len(raw)}（既不是 8 也不是 12），"
            "该驱动返回的不是 seek-penalty 描述符"
        )
        expected = bool(raw[8] if len(raw) == 12 else raw[4])
        assert expected == probe.incurs_seek_penalty, (
            f"{info.letter}: 独立复算 incurs={expected} 与被测实现 "
            f"{probe.incurs_seek_penalty} 不一致（raw={raw.hex()}）"
        )

    # 本机至少有一块盘支持该属性；一块都不支持说明驱动全线不支持，
    # 那就无法验证介质判定，必须失败而不是"静默通过"（原先是恒真的 `>= 0`）
    assert probed >= 1, "本机没有任何卷支持 SeekPenalty 查询，介质判定无法交叉验证"



if __name__ == "__main__":
    from tests._harness import exit_with

    exit_with(globals())
