"""FILE 记录与属性解析。

三处最容易写错、写错又不报错的地方，这里逐条守住：

1. **fixup（更新序列号）必须先还原再解析**。NTFS 把每个扇区末尾 2 字节替换成
   USN，真实值存在记录头的 USA 数组里。不还原的话，每个 512 字节边界上都会
   有 2 字节脏数据——解析出来的属性长度、runlist 都可能是对的（因为不在边界
   上），只在记录跨扇区时才突然错，属于最难查的一类。
2. **resident / non-resident 的属性头布局不同**。``attribute_id`` 都在 0x0E，
   但 resident 的 value 在 0x10/0x14，non-resident 的 VCN 在 0x10/0x18、
   runlist 偏移在 0x20。混用会把 runlist 偏移读成 0，然后解出空区间表 ——
   文件看起来"不占空间"，整理时被静默跳过。
3. **文件名有多个命名空间**。同一文件常有 ``WIN32`` 与 ``DOS 8.3`` 两个名字
   （甚至加上 POSIX）。取 DOS 名会让路径变成 ``PROGRA~1``，后续按路径打开
   文件仍然能成功，但用户在界面上看到的是 8.3 短名 —— 不致命，但很难解释。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ..contract.models import Extent
from .runlist import Runlist, RunlistError, decode_runs

__all__ = [
    "ATTR",
    "FileRecord",
    "FileNameAttr",
    "NtfsAttribute",
    "NAMESPACE_WIN32",
    "NAMESPACE_WIN32_DOS",
    "apply_fixups",
    "parse_record",
]

RECORD_SIGNATURE = b"FILE"
ATTR_END = 0xFFFFFFFF


class _Attr:
    """属性类型常量。用类而不是散落的模块级名字，便于 starmap/枚举。"""

    STANDARD_INFORMATION = 0x10
    ATTRIBUTE_LIST = 0x20
    FILE_NAME = 0x30
    OBJECT_ID = 0x40
    SECURITY_DESCRIPTOR = 0x50
    VOLUME_NAME = 0x60
    VOLUME_INFORMATION = 0x70
    DATA = 0x80
    INDEX_ROOT = 0x90
    INDEX_ALLOCATION = 0xA0
    BITMAP = 0xB0
    REPARSE_POINT = 0xC0
    EA_INFORMATION = 0xD0
    EA = 0xE0
    LOGGED_UTILITY_STREAM = 0x100


ATTR = _Attr

NAMESPACE_POSIX = 0
NAMESPACE_WIN32 = 1
NAMESPACE_DOS = 2
NAMESPACE_WIN32_DOS = 3

#: STANDARD_INFORMATION 里的文件属性位
FILE_ATTR_HIDDEN = 0x0002
FILE_ATTR_SYSTEM = 0x0004
FILE_ATTR_REPARSE = 0x0400
FILE_ATTR_COMPRESSED = 0x0800
FILE_ATTR_ENCRYPTED = 0x4000

# 属性头上通用的 flags
ATTR_FLAG_COMPRESSED = 0x0001
ATTR_FLAG_ENCRYPTED = 0x4000
ATTR_FLAG_SPARSE = 0x8000


@dataclass(frozen=True, slots=True)
class FileNameAttr:
    parent_ref: int
    name: str
    namespace: int
    flags: int = 0


@dataclass(frozen=True, slots=True)
class NtfsAttribute:
    type: int
    non_resident: bool
    name: str = ""
    flags: int = 0
    value: bytes = b""
    runlist: Runlist | None = None
    real_size: int = 0

    @property
    def is_compressed(self) -> bool:
        return bool(self.flags & ATTR_FLAG_COMPRESSED)

    @property
    def is_encrypted(self) -> bool:
        return bool(self.flags & ATTR_FLAG_ENCRYPTED)

    @property
    def is_sparse(self) -> bool:
        return bool(self.flags & ATTR_FLAG_SPARSE)


@dataclass(frozen=True, slots=True)
class FileRecord:
    """一条 MFT 记录的解析结果（只保留整理需要的字段）。"""

    number: int
    sequence: int
    in_use: bool
    is_dir: bool
    base_ref: int
    names: tuple[FileNameAttr, ...] = ()
    data: NtfsAttribute | None = None
    si_flags: int = 0
    has_reparse: bool = False
    has_attribute_list: bool = False
    attrs_seen: tuple[int, ...] = field(default=())

    # -- 便捷视图 ---------------------------------------------------------

    @property
    def best_name(self) -> FileNameAttr | None:
        """优先 WIN32 / WIN32&DOS，其次 POSIX，最后 DOS 短名。"""
        if not self.names:
            return None
        for ns in (NAMESPACE_WIN32_DOS, NAMESPACE_WIN32, NAMESPACE_POSIX, NAMESPACE_DOS):
            for item in self.names:
                if item.namespace is ns:
                    return item
        return self.names[0]

    @property
    def parent_ref(self) -> int:
        name = self.best_name
        return name.parent_ref if name else 0

    @property
    def name(self) -> str:
        name = self.best_name
        return name.name if name else ""

    @property
    def extents(self) -> tuple[Extent, ...]:
        if self.data is None or self.data.runlist is None:
            return ()
        return self.data.runlist.extents

    @property
    def size_bytes(self) -> int:
        if self.data is None:
            return 0
        if self.data.non_resident:
            return self.data.real_size
        return len(self.data.value)

    @property
    def is_system_file(self) -> bool:
        return bool(self.si_flags & FILE_ATTR_SYSTEM)

    @property
    def is_reparse(self) -> bool:
        return self.has_reparse or bool(self.si_flags & FILE_ATTR_REPARSE)


# --------------------------------------------------------------------------
# fixup
# --------------------------------------------------------------------------


def apply_fixups(raw: bytearray, bytes_per_sector: int) -> bool:
    """还原扇区末尾被替换的 2 字节。返回是否成功。

    失败不抛异常：坏记录在真实磁盘上很常见（掉电、坏道），一条坏记录不该
    让整卷扫描失败。返回 False 让调用方跳过它并计数。
    """
    if len(raw) < 0x30:
        return False
    usa_ofs, usa_count = struct.unpack_from("<HH", raw, 0x04)
    if usa_count == 0 or usa_ofs + usa_count * 2 > len(raw):
        return False
    # 期望 usa_count = 记录扇区数 + 1
    expected = len(raw) // bytes_per_sector + 1
    if usa_count != expected:
        return False

    usn = bytes(raw[usa_ofs : usa_ofs + 2])
    for i in range(1, usa_count):
        pos = i * bytes_per_sector - 2
        if pos + 2 > len(raw):
            return False
        if raw[pos : pos + 2] != usn:
            # USN 不匹配 = 这条记录在更新中被撕裂，内容不可信
            return False
        raw[pos : pos + 2] = raw[usa_ofs + i * 2 : usa_ofs + i * 2 + 2]
    return True


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


#: 属性头通用部分（0x00~0x10）：type / length / non_resident / name_len /
#: name_off / flags / attr_id。一次 unpack 取全，别为 1~2 字节的字段反复调用。
_ATTR_HEADER = struct.Struct("<IIBBHHH")
#: non-resident 头：start_vcn(0x10) / last_vcn(0x18) / runlist_off(0x20)，
#: 中间 14 字节是压缩/分配尺寸，跳到 real_size(0x30)。一次取全。
_ATTR_NONRES = struct.Struct("<qqH14xq")
#: resident 头：value_len(0x10) / value_off(0x14)
_ATTR_RES = struct.Struct("<IH")
#: FILE 记录头（0x10~0x28）：sequence / link / attrs_off / flags / used /
#: allocated / base_ref
_RECORD_HEADER = struct.Struct("<HHHHIIQ")
_U32 = struct.Struct("<I")
#: FILE_NAME 值头：parent_ref(0x00) / flags(0x38) / name_len(0x40) / ns(0x41)
_FILE_NAME_HEAD = struct.Struct("<Q48xI4xBB")

#: 真正需要解析的属性类型。其余类型只看 type/length 就跳过 —— 它们的 value
#: 对"这个文件能不能整理"没有任何影响（见 catalog 的跳过策略）。
_WANTED = frozenset(
    {
        _Attr.STANDARD_INFORMATION,
        _Attr.ATTRIBUTE_LIST,
        _Attr.FILE_NAME,
        _Attr.DATA,
        _Attr.REPARSE_POINT,
    }
)


def _parse_attribute(raw: bytes | bytearray, pos: int, limit: int | None = None) -> tuple[NtfsAttribute, int] | None:
    """解析一条属性。

    ``raw`` 故意允许 ``bytearray``：调用方传的是**同一条记录的可变副本**。
    这里只把真正要留下的 value 片段转成 ``bytes``（几十到一两百字节），
    不能反过来要求调用方先 ``bytes(buf)`` —— 那是每条属性复制一整块 1 KiB
    记录，一个典型记录有 4~6 条属性，等于每次解析多拷 5 份。
    """
    n = len(raw) if limit is None else limit
    if pos + 16 > n:
        return None
    attr_type, length, non_resident, name_len, name_off, flags, _attr_id = (
        _ATTR_HEADER.unpack_from(raw, pos)
    )
    if attr_type == ATTR_END or length < 16 or pos + length > n:
        return None

    name = ""
    if name_len and 0 < name_off and pos + name_off + name_len * 2 <= n:
        name = raw[pos + name_off : pos + name_off + name_len * 2].decode(
            "utf-16-le", errors="replace"
        )

    if non_resident == 0:
        if pos + 0x16 > n:
            return None
        value_len, value_off = _ATTR_RES.unpack_from(raw, pos + 0x10)
        start = pos + value_off
        end = min(n, start + value_len)
        value = bytes(raw[start:end]) if 0 < value_off and start < n else b""
        return (
            NtfsAttribute(
                type=attr_type,
                non_resident=False,
                name=name,
                flags=flags,
                value=value,
                real_size=len(value),
            ),
            pos + length,
        )

    # non-resident
    if pos + 0x40 > n:
        return None
    start_vcn, last_vcn, run_off, real_size = _ATTR_NONRES.unpack_from(raw, pos + 0x10)

    runlist: Runlist | None = None
    run_start = pos + run_off
    if 0 < run_off and run_start < pos + length and run_start < n:
        blob = raw[run_start : pos + length]
        try:
            runlist = decode_runs(blob)
        except RunlistError:
            runlist = None

    # 只保留第一个 extent（start_vcn == 0）的属性；后续片段属于同一属性，
    # 完整合并需要读 ATTRIBUTE_LIST，本版对这类文件直接跳过（见 catalog）
    return (
        NtfsAttribute(
            type=attr_type,
            non_resident=True,
            name=name,
            flags=flags,
            runlist=runlist,
            real_size=max(0, real_size),
        ),
        pos + length,
    )


def _parse_file_name(value: bytes) -> FileNameAttr | None:
    if len(value) < 0x42:
        return None
    parent_ref, flags, name_len, namespace = _FILE_NAME_HEAD.unpack_from(value, 0)
    if 0x42 + name_len * 2 > len(value):
        return None
    name = value[0x42 : 0x42 + name_len * 2].decode("utf-16-le", errors="replace")
    return FileNameAttr(
        parent_ref=parent_ref & 0x0000FFFFFFFFFFFF,  # 低 48 位才是记录号
        name=name,
        namespace=namespace,
        flags=flags,
    )


def parse_record(
    raw: bytes, number: int, bytes_per_sector: int = 512
) -> FileRecord | None:
    """解析一条 FILE 记录。结构不可信时返回 ``None``（调用方计为坏记录）。"""
    if len(raw) < 0x30 or raw[:4] != RECORD_SIGNATURE:
        return None

    buf = bytearray(raw)
    if not apply_fixups(buf, bytes_per_sector):
        return None

    if len(buf) < 0x28:
        return None
    sequence, _links, attrs_offset, flags, used_size, _allocated, base_ref = (
        _RECORD_HEADER.unpack_from(buf, 0x10)
    )
    base_ref &= 0x0000FFFFFFFFFFFF

    in_use = bool(flags & 0x01)
    is_dir = bool(flags & 0x02)

    if not in_use:
        # 已删除的记录槽：属性区是上一次使用的残留，随时可能被复用或覆盖，
        # 读它没有任何意义。目录服务本来也会丢掉这些记录（见 catalog），
        # 所以这里只把头部交出去 —— 在 R: 这种"删得多"的卷上，这一步能省掉
        # 八成记录的属性遍历。
        return FileRecord(
            number=number,
            sequence=sequence,
            in_use=False,
            is_dir=is_dir,
            base_ref=base_ref,
        )

    limit = min(len(buf), used_size if used_size > attrs_offset else len(buf))
    names: list[FileNameAttr] = []
    data_attr: NtfsAttribute | None = None
    si_flags = 0
    has_reparse = False
    has_attr_list = False
    seen: list[int] = []

    pos = attrs_offset
    while pos + 16 <= limit:
        attr_type = _U32.unpack_from(buf, pos)[0]
        if attr_type == ATTR_END:
            break
        length = _U32.unpack_from(buf, pos + 4)[0]
        if length < 16 or pos + length > limit:
            break
        seen.append(attr_type)
        # 只有这五种属性会影响"能不能整理"；其余（索引、安全描述符、EA、
        # 对象 ID…）连 value 都不用拷。目录记录的 INDEX_ROOT 常有几 KB，
        # 每个都复制一遍是纯浪费 —— 而目录在 MFT 里占很大比例。
        if attr_type in _WANTED:
            parsed = _parse_attribute(buf, pos, limit)
            if parsed is None:
                break
            attr, next_pos = parsed
            if next_pos <= pos:
                break
            pos = next_pos
        else:
            pos += length
            continue

        if attr.type == ATTR.STANDARD_INFORMATION and not attr.non_resident:
            if len(attr.value) >= 0x24:
                si_flags = struct.unpack_from("<I", attr.value, 0x20)[0]
        elif attr.type == ATTR.FILE_NAME and not attr.non_resident:
            parsed_name = _parse_file_name(attr.value)
            if parsed_name is not None:
                names.append(parsed_name)
        elif attr.type == ATTR.DATA and not attr.name:
            # 只认**无名** DATA（主数据流）；命名流是备用数据，不动
            if data_attr is None:
                data_attr = attr
        elif attr.type == ATTR.REPARSE_POINT:
            has_reparse = True
        elif attr.type == ATTR.ATTRIBUTE_LIST:
            has_attr_list = True

    return FileRecord(
        number=number,
        sequence=sequence,
        in_use=in_use,
        is_dir=is_dir,
        base_ref=base_ref,
        names=tuple(names),
        data=data_attr,
        si_flags=si_flags,
        has_reparse=has_reparse,
        has_attribute_list=has_attr_list,
        attrs_seen=tuple(seen),
    )
