"""data run 解码：NTFS 用来表达「这段虚拟簇落在哪些物理簇」的压缩编码。

格式（每个 run）：1 字节头 + 变长长度 + 变长偏移

* 头字节低 4 位 = 长度字段的字节数（1~15）
* 头字节高 4 位 = 偏移字段的字节数（0~15，**0 表示稀疏洞**）
* 偏移是**相对上一个 run 末尾**的有符号增量，不是绝对地址

三个必须处理对的点，错一个都会算出错误的 LCN：

1. 偏移字段是**有符号**的（可能向后跳）；
2. 头字节为 0 表示 runlist 结束；
3. 偏移字节数为 0 的 run 是**稀疏**（全零区），它不占物理簇。

稀疏区一旦被当成普通 run，就会把"文件里有一段洞"错算成"这文件占了很多
物理簇"，进而让规划器去搬根本不存在的数据。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contract.models import Extent

__all__ = ["Runlist", "decode_runs", "encode_runs"]


class RunlistError(ValueError):
    """runlist 结构损坏。"""


@dataclass(frozen=True, slots=True)
class Runlist:
    """解码结果：连续的物理区间 + 是否含稀疏洞。"""

    extents: tuple[Extent, ...]
    has_sparse: bool
    #: 逻辑长度（含稀疏洞的簇数），用于校验 real_size
    total_clusters: int

    @property
    def ok_for_move(self) -> bool:
        """是否适合用 FSCTL_MOVE_FILE 搬。

        稀疏/压缩/加密的文件，其逻辑簇数与物理簇数不对应，搬簇的语义会变，
        本工具一律不碰——这不是能力不足，是有意为之。
        """
        return not self.has_sparse


def decode_runs(data: bytes) -> Runlist:
    """把 runlist 字节流解成物理区间表。"""
    extents: list[Extent] = []
    pos = 0
    lcn = 0
    total = 0
    sparse = False
    n = len(data)

    while pos < n:
        header = data[pos]
        pos += 1
        if header == 0:
            break
        len_size = header & 0x0F
        off_size = (header >> 4) & 0x0F
        if len_size == 0 or len_size > 8 or off_size > 8:
            raise RunlistError(f"run 头字节非法：0x{header:02X} @ {pos - 1}")
        if pos + len_size + off_size > n:
            raise RunlistError("runlist 越界（尾部被截断）")

        length = int.from_bytes(data[pos : pos + len_size], "little", signed=False)
        pos += len_size
        if length <= 0:
            raise RunlistError(f"run 长度非法：{length}")

        if off_size == 0:
            # 稀疏洞：逻辑上占 length 个簇，物理上不占
            sparse = True
            total += length
            continue

        delta = int.from_bytes(data[pos : pos + off_size], "little", signed=True)
        pos += off_size
        lcn += delta
        if lcn < 0:
            raise RunlistError(f"LCN 解出负值：{lcn}")

        extents.append(Extent(lcn, length))
        total += length

    return Runlist(extents=tuple(extents), has_sparse=sparse, total_clusters=total)


def encode_runs(extents: list[Extent]) -> bytes:
    """把区间表编码回 runlist（只用于构造测试镜像与回写校验）。

    偏移的基准是**上一段 run 的起始 LCN**，不是上一段的末尾。这一条一旦搞错，
    第二段起就会整体偏移「上一段长度」个簇——读出来的是别人家的数据，而且
    结构完全合法，不会报任何错。
    """
    out = bytearray()
    prev_start = 0
    for ext in extents:
        if ext.length <= 0:
            continue
        length_bytes = _minimal_unsigned(ext.length)
        delta = ext.start - prev_start
        offset_bytes = _minimal_signed(delta)
        out.append(((len(offset_bytes) & 0x0F) << 4) | (len(length_bytes) & 0x0F))
        out += length_bytes
        out += offset_bytes
        prev_start = ext.start
    out.append(0)
    return bytes(out)


def _minimal_unsigned(value: int) -> bytes:
    if value <= 0:
        raise ValueError("长度必须为正")
    size = (value.bit_length() + 7) // 8
    return value.to_bytes(max(1, size), "little", signed=False)


def _minimal_signed(value: int) -> bytes:
    # 有符号最小字节数：最高位不能与符号位冲突
    for size in range(1, 9):
        lo = -(1 << (size * 8 - 1))
        hi = (1 << (size * 8 - 1)) - 1
        if lo <= value <= hi:
            return value.to_bytes(size, "little", signed=True)
    raise ValueError(f"偏移无法编码：{value}")
