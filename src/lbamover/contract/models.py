"""契约层数据模型。

设计约束（都是踩过坑才写下的）：

1. **全部 frozen dataclass**。引擎在多个线程里同时读取计划与报告，
   任何可变共享状态都是竞态的温床。
2. **不做隐式单位换算**。LBA 一律用「块」为单位，字节只在模型属性里
   派生。旧版混用 sector/cluster/byte 三种单位，出现过差 8 倍的越界。
3. **可序列化**。``Plan`` 必须能落盘并被断点文件反查，因此提供
   ``to_dict / from_dict``，且 ``plan_hash`` 只由内容决定。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

from .enums import ErrorClass, FreeSpaceSide, Strategy, VolumeKind

__all__ = [
    "Extent",
    "FailureDetail",
    "FileFrag",
    "FragReport",
    "Bucket",
    "Geometry",
    "JobParams",
    "JobSpec",
    "MoveOp",
    "Plan",
    "Progress",
    "VolumeInfo",
    "normalize_extents",
    "merge_extents",
]


# --------------------------------------------------------------------------
# 区间
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class Extent:
    """一段连续 LBA 区间，单位为块。``[start, start+length)``。

    ``start`` 为 0 且 ``length`` 为 0 是唯一允许的空区间表示（用于占位），
    其余情况都必须 ``start >= 0 and length > 0``——静默接受负长度会让
    下游的 ``end`` 计算出一个假的右端点，越界写就是这么来的。
    """

    start: int
    length: int

    def __post_init__(self) -> None:
        if self.length < 0:
            raise ValueError(f"extent length 不能为负: {self.length}")
        if self.start < 0:
            raise ValueError(f"extent start 不能为负: {self.start}")
        if self.length == 0 and self.start != 0:
            raise ValueError("空 extent 只能用 start=0,length=0 表示")

    @property
    def end(self) -> int:
        """右端点（开区间）。空区间时等于 ``start``。"""
        return self.start + self.length

    @property
    def is_empty(self) -> bool:
        return self.length == 0

    def contains(self, lba: int) -> bool:
        return self.start <= lba < self.end

    def overlaps(self, other: "Extent") -> bool:
        if self.is_empty or other.is_empty:
            return False
        return self.start < other.end and other.start < self.end

    def adjacent(self, other: "Extent") -> bool:
        """首尾相接（可无损合并）。"""
        if self.is_empty or other.is_empty:
            return False
        return self.end == other.start or other.end == self.start

    def merged_with(self, other: "Extent") -> "Extent":
        if not (self.adjacent(other) or self.overlaps(other)):
            raise ValueError(f"区间不相接也无法合并: {self} / {other}")
        lo = min(self.start, other.start)
        hi = max(self.end, other.end)
        return Extent(lo, hi - lo)

    def to_dict(self) -> dict[str, int]:
        return {"start": self.start, "length": self.length}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Extent":
        return cls(int(raw["start"]), int(raw["length"]))

    def __str__(self) -> str:  # pragma: no cover - 仅日志可读性
        return f"[{self.start}, {self.end})"


def merge_extents(extents: Iterable[Extent]) -> list[Extent]:
    """合并重叠与相邻区间，返回按 ``start`` 升序的规范形式。

    重叠视为可合并而非报错：MFT 数据在边界上偶尔给出首尾相接的
    run，直接报错会让整卷分析失败，得不偿失。
    """
    items = [e for e in extents if not e.is_empty]
    if not items:
        return []
    items.sort(key=lambda e: (e.start, e.end))
    out: list[Extent] = [items[0]]
    for cur in items[1:]:
        last = out[-1]
        if cur.start <= last.end:  # 相邻或重叠
            out[-1] = Extent(last.start, max(last.end, cur.end) - last.start)
        else:
            out.append(cur)
    return out


def normalize_extents(extents: Iterable[Extent]) -> tuple[Extent, ...]:
    """同 :func:`merge_extents`，返回元组（不可变，适合放进 frozen 模型）。"""
    return tuple(merge_extents(extents))


# --------------------------------------------------------------------------
# 几何与卷
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Geometry:
    """块设备几何。``block_size`` 是引擎的原子读写单位。"""

    block_size: int
    block_count: int

    def __post_init__(self) -> None:
        if self.block_size <= 0:
            raise ValueError(f"block_size 必须为正: {self.block_size}")
        if self.block_count < 0:
            raise ValueError(f"block_count 不能为负: {self.block_count}")

    @property
    def total_bytes(self) -> int:
        return self.block_size * self.block_count

    def contains(self, ext: Extent) -> bool:
        return ext.start >= 0 and ext.end <= self.block_count

    def to_dict(self) -> dict[str, int]:
        return {"block_size": self.block_size, "block_count": self.block_count}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Geometry":
        return cls(int(raw["block_size"]), int(raw["block_count"]))


@dataclass(frozen=True, slots=True)
class VolumeInfo:
    """一个可操作的目标卷/分区。UI 的「目标选择」直接渲染这个结构。"""

    device_path: str           # 例如 r"\\.\D:" 或某个镜像文件路径
    mount_point: str           # 例如 "D:\"
    label: str
    filesystem: str
    kind: VolumeKind
    geometry: Geometry
    free_bytes: int = 0
    is_system: bool = False
    is_mounted: bool = True
    readable: bool = True
    writable: bool = False
    note: str = ""

    @property
    def letter(self) -> str:
        mp = self.mount_point.rstrip("\\/")
        return mp if len(mp) <= 2 else self.mount_point

    @property
    def free_blocks(self) -> int:
        if self.geometry.block_size == 0:
            return 0
        return self.free_bytes // self.geometry.block_size

    @property
    def used_blocks(self) -> int:
        return max(0, self.geometry.block_count - self.free_blocks)

    @property
    def risky(self) -> bool:
        """需要额外确认闸门的卷：系统卷、或已挂载且以原始块方式写入。"""
        return self.is_system or (self.is_mounted and self.writable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_path": self.device_path,
            "mount_point": self.mount_point,
            "label": self.label,
            "filesystem": self.filesystem,
            "kind": self.kind.value,
            "geometry": self.geometry.to_dict(),
            "free_bytes": self.free_bytes,
            "is_system": self.is_system,
            "is_mounted": self.is_mounted,
            "readable": self.readable,
            "writable": self.writable,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VolumeInfo":
        return cls(
            device_path=raw["device_path"],
            mount_point=raw["mount_point"],
            label=raw.get("label", ""),
            filesystem=raw.get("filesystem", ""),
            kind=VolumeKind(raw.get("kind", "unknown")),
            geometry=Geometry.from_dict(raw["geometry"]),
            free_bytes=int(raw.get("free_bytes", 0)),
            is_system=bool(raw.get("is_system", False)),
            is_mounted=bool(raw.get("is_mounted", True)),
            readable=bool(raw.get("readable", True)),
            writable=bool(raw.get("writable", False)),
            note=raw.get("note", ""),
        )


# --------------------------------------------------------------------------
# 碎片
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileFrag:
    """单个文件的 LBA 分布。``extents`` 必须是已规范化的规范化区间。"""

    file_id: str
    path: str
    extents: tuple[Extent, ...]
    attributes: int = 0

    def __post_init__(self) -> None:
        total = 0
        prev_end = -1
        for ext in self.extents:
            if ext.length <= 0:
                raise ValueError(f"{self.path}: extent 长度必须为正")
            if ext.start < prev_end:
                raise ValueError(f"{self.path}: extents 必须升序且不重叠")
            prev_end = ext.end
            total += ext.length

    @property
    def total_blocks(self) -> int:
        return sum(e.length for e in self.extents)

    @property
    def run_count(self) -> int:
        return len([e for e in self.extents if e.length > 0])

    @property
    def largest_run(self) -> int:
        return max((e.length for e in self.extents), default=0)

    @property
    def frag_score(self) -> float:
        """碎片度，``0.0`` 为完全连续，趋近 ``1.0`` 为极度碎片。

        定义：``1 - largest_run / total_blocks``。

        选这个而不是「段数 / 文件数」，是因为它对**大文件被切成两半**
        这种最常见、危害最大的情况敏感，而对「一堆小 extent 里最大那个
        其实很大」的情况不过度惩罚——后者整理收益本来就不高。
        """
        total = self.total_blocks
        if total <= 0 or self.run_count <= 1:
            return 0.0
        return max(0.0, 1.0 - self.largest_run / total)

    @property
    def is_fragmented(self) -> bool:
        return self.run_count > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "path": self.path,
            "extents": [e.to_dict() for e in self.extents],
            "attributes": self.attributes,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FileFrag":
        return cls(
            file_id=raw["file_id"],
            path=raw["path"],
            extents=tuple(Extent.from_dict(e) for e in raw["extents"]),
            attributes=int(raw.get("attributes", 0)),
        )


@dataclass(frozen=True, slots=True)
class Bucket:
    """可视化用的等宽 LBA 分桶。UI 的碎片分布图直接消费这个结构。

    前端不需要自己再算一遍几何，避免「图上画的和引擎算的不是一回事」。
    """

    index: int
    start: int
    end: int
    used_blocks: int
    free_blocks: int
    extent_count: int

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def usage_ratio(self) -> float:
        return 0.0 if self.length == 0 else self.used_blocks / self.length

    @property
    def frag_ratio(self) -> float:
        """桶内 extent 密度归一化到 0..1。

        取 ``extent_count / length`` 再按"平均每 4 块一个 extent 边界"为满值。

        标定依据：一个 32 块的桶里只有一个 extent，是**健康**的，不该显示成
        热区。之前按 1/8 标定，任何桶只要有 1 个 extent 就是 25% 热度，整条
        热力带永远是灰黄色，等于没有信息。
        """
        if self.length <= 0:
            return 0.0
        return min(1.0, (self.extent_count / self.length) * 4.0)


@dataclass(frozen=True, slots=True)
class FragReport:
    """一次扫描的完整结果。既喂给规划器，也喂给 UI。"""

    geometry: Geometry
    files: tuple[FileFrag, ...] = ()
    free_extents: tuple[Extent, ...] = ()
    buckets: tuple[Bucket, ...] = ()
    elapsed_s: float = 0.0
    truncated: bool = False
    note: str = ""
    #: 扫描走的是**受限路径**（未提权）：看不到 ``$Bitmap``，自由空间由已用
    #: 区间反推，打不开的文件也不在内。这样的报告够用来"看"，**不够用来改**，
    #: 因此引擎会拒绝在受限报告上执行。
    restricted: bool = False
    #: 被保护策略拦下、**本次不会搬**的文件的区间。
    #:
    #: 单独记一份而不是从 ``files`` 里推断，是因为「可用空间整理」必须知道
    #: 目标区里哪些块是**搬不动**的：把 ``pagefile.sys`` 或 ``$MFT`` 算成
    #: "能腾出来"，用户会得到一个永远达不到的承诺。
    protected_extents: tuple[Extent, ...] = ()

    # -- 派生指标 ---------------------------------------------------------

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_extents(self) -> int:
        return sum(f.run_count for f in self.files)

    @property
    def fragmented_files(self) -> tuple[FileFrag, ...]:
        return tuple(f for f in self.files if f.is_fragmented)

    @property
    def file_frag_score(self) -> float:
        """按块数加权的文件碎片度。空卷返回 0。"""
        total = sum(f.total_blocks for f in self.files)
        if total <= 0:
            return 0.0
        weighted = sum(f.frag_score * f.total_blocks for f in self.files)
        return weighted / total

    @property
    def free_run_count(self) -> int:
        return len([e for e in self.free_extents if e.length > 0])

    @property
    def largest_free_run(self) -> int:
        return max((e.length for e in self.free_extents), default=0)

    @property
    def free_blocks(self) -> int:
        return sum(e.length for e in self.free_extents)

    @property
    def free_frag_score(self) -> float:
        """自由空间碎片度：``1 - largest_free_run / free_blocks``。

        这是「还能不能放下一个大文件」的直接指标，比文件碎片度更能说明
        用户的实际痛点。
        """
        total = self.free_blocks
        if total <= 0 or self.free_run_count <= 1:
            return 0.0
        return max(0.0, 1.0 - self.largest_free_run / total)

    def worst(self, limit: int = 10) -> tuple[FileFrag, ...]:
        """按「可回收的搬运块数」降序，返回最值得整理的文件。

        排序键 ``(run_count - 1) * avg_run`` 近似等于「整理后能省下的连续
        段数成本」，比单纯按 frag_score 排序更贴近收益。
        """
        ranked = sorted(
            self.fragmented_files,
            key=lambda f: (f.run_count - 1) * (f.total_blocks / f.run_count),
            reverse=True,
        )
        return tuple(ranked[: max(0, limit)])

    def file_by_id(self, file_id: str) -> FileFrag | None:
        for f in self.files:
            if f.file_id == file_id:
                return f
        return None


# --------------------------------------------------------------------------
# 计划
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MoveOp:
    """一次「把某个文件的所有 extent 搬进一段连续目标区」的操作。

    为什么按文件而不是按块组织：块级的自由搬运会把同一个文件拆得更碎，
    而且没有文件边界就无法做原子性判断。文件是能说明「搬完是否成功」的
    最小单位。
    """

    op_id: int
    file_id: str
    path: str
    src_extents: tuple[Extent, ...]
    dst_start: int
    verify: bool = True

    def __post_init__(self) -> None:
        if self.dst_start < 0:
            raise ValueError(f"dst_start 不能为负: {self.dst_start}")
        if not self.src_extents:
            raise ValueError(f"op {self.op_id}: src_extents 不能为空")

    @property
    def blocks(self) -> int:
        return sum(e.length for e in self.src_extents)

    @property
    def dst_extent(self) -> Extent:
        return Extent(self.dst_start, self.blocks)

    @property
    def src_span(self) -> Extent:
        """源区间的外接范围（含中间的洞）。用于重叠检测与日志。"""
        return Extent(
            min(e.start for e in self.src_extents),
            max(e.end for e in self.src_extents) - min(e.start for e in self.src_extents),
        )

    @property
    def self_overlapping(self) -> bool:
        """目标区与源区间有交集。

        同盘原始块搬运时这会导致读到的数据已被自己覆盖，必须由规划器
        提前排除，运行期再兜底断言。
        """
        dst = self.dst_extent
        return any(dst.overlaps(src) for src in self.src_extents)

    def to_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "file_id": self.file_id,
            "path": self.path,
            "src_extents": [e.to_dict() for e in self.src_extents],
            "dst_start": self.dst_start,
            "verify": self.verify,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MoveOp":
        return cls(
            op_id=int(raw["op_id"]),
            file_id=raw["file_id"],
            path=raw["path"],
            src_extents=tuple(Extent.from_dict(e) for e in raw["src_extents"]),
            dst_start=int(raw["dst_start"]),
            verify=bool(raw.get("verify", True)),
        )


@dataclass(frozen=True, slots=True)
class FailureDetail:
    """一个文件的失败详情。

    存在的唯一目的：让用户能**原样复制**给 AI 或贴进 issue，而不用自己
    描述"它在搬某个文件的时候报错了"。所以每个字段都要么能让人看懂，
    要么能让下一个接手的人定位：阶段说明"卡在哪一步"，``winerror`` 说明
    "系统到底说了什么"，``error_class`` 说明"该重试还是该改环境"。

    旧版只往日志里写一行 ``op12 失败：R:\\a.pst — 拒绝访问``，用户既不知道
    是打开文件失败还是搬簇失败，也不知道是权限问题还是共享冲突 —— 而这两类
    问题的处理方式完全不同。
    """

    path: str
    op_id: int
    #: 出错的阶段：打开文件 / 复核布局 / 搬簇 / 读取数据 / 写入数据 / 回读校验
    stage: str = ""
    #: 人类可读的一句话原因
    reason: str = ""
    #: 异常类名，便于搜代码
    error_type: str = ""
    #: ``retryable`` / ``fatal`` / ``verify_mismatch`` / ``cancelled``
    error_class: str = ""
    #: Windows 错误码。0 表示不适用（非系统调用失败）
    winerror: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "path": self.path,
            "stage": self.stage,
            "reason": self.reason,
            "error_type": self.error_type,
            "error_class": self.error_class,
            "winerror": self.winerror,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FailureDetail":
        return cls(
            path=str(raw.get("path", "")),
            op_id=int(raw.get("op_id", -1)),
            stage=str(raw.get("stage", "")),
            reason=str(raw.get("reason", "")),
            error_type=str(raw.get("error_type", "")),
            error_class=str(raw.get("error_class", "")),
            winerror=int(raw.get("winerror", 0)),
        )

    @property
    def error_class_label(self) -> str:
        """错误分类的中文标签；无法识别时原样返回。"""
        try:
            return ErrorClass(self.error_class).label
        except ValueError:
            return self.error_class

    def one_line(self) -> str:
        """单行摘要，用于日志与归并。"""
        where = f"[{self.stage}] " if self.stage else ""
        code = f" winerror={self.winerror}" if self.winerror else ""
        kind = f"（{self.error_class_label}）" if self.error_class else ""
        return f"{where}{self.reason}{code}{kind}"


@dataclass(frozen=True, slots=True)
class Plan:
    """一次整理的完整执行计划。可落盘、可校验、可增量续做。"""

    job_id: str
    strategy: Strategy
    ops: tuple[MoveOp, ...] = ()
    feasible: bool = True
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    reserved_tail: int = 0
    #: 计划里是否有 op 的目标区落在「更早的 op 的源区」上（等于要复用搬走
    #: 腾出来的空间）。这类计划必须串行执行，否则后一个 op 可能在前一个
    #: op 读完之前覆盖它的源。规划器负责置位，调度器负责遵守。
    requires_serial: bool = False
    #: 「可用空间整理」最终腾出的连续空闲区（块）。别的策略为 ``None``。
    #: 计划里带着它，是因为"用户要的那段空间到底在哪、有多大"必须能被
    #: 执行前的审计与界面复核 —— 只写进 notes 字符串就没法校验。
    reclaim_extent: Extent | None = None

    @property
    def total_blocks(self) -> int:
        return sum(op.blocks for op in self.ops)

    @property
    def op_count(self) -> int:
        return len(self.ops)

    @property
    def plan_hash(self) -> str:
        """计划内容的稳定指纹，用于断点文件的乐观校验。

        只包含会影响执行结果的内容（op 顺序、源、目标），不含时间戳，
        保证同一份计划重算两次得到同一个 hash。
        """
        payload = {
            "job_id": self.job_id,
            "strategy": self.strategy.value,
            "ops": [op.to_dict() for op in self.ops],
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "strategy": self.strategy.value,
            "ops": [op.to_dict() for op in self.ops],
            "feasible": self.feasible,
            "reasons": list(self.reasons),
            "notes": list(self.notes),
            "reserved_tail": self.reserved_tail,
            "requires_serial": self.requires_serial,
            "reclaim_extent": (
                None if self.reclaim_extent is None else self.reclaim_extent.to_dict()
            ),
            "plan_hash": self.plan_hash,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Plan":
        reclaim = raw.get("reclaim_extent")
        return cls(
            job_id=raw["job_id"],
            strategy=Strategy(raw["strategy"]),
            ops=tuple(MoveOp.from_dict(o) for o in raw.get("ops", ())),
            feasible=bool(raw.get("feasible", True)),
            reasons=tuple(raw.get("reasons", ())),
            notes=tuple(raw.get("notes", ())),
            reserved_tail=int(raw.get("reserved_tail", 0)),
            requires_serial=bool(raw.get("requires_serial", False)),
            reclaim_extent=None if reclaim is None else Extent.from_dict(reclaim),
        )


# --------------------------------------------------------------------------
# 参数、任务、进度
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobParams:
    """运行参数。UI 的「参数配置」面板映射到这里，非法值在构造期就炸。

    内存上界由这个结构自己算得出来：``chunk_blocks * block_size *
    queue_depth * concurrency``。这是「内存可控」的**唯一**依据，任何
    新增的缓冲都必须计入这里。
    """

    #: 单块大小（字节），也是流水线的原子传输单位。
    block_size: int = 4096
    #: 一次连续读写的块数。越大吞吐越高、单次延迟越大。
    chunk_blocks: int = 256
    #: 流水线队列深度（在途 chunk 数）。>=2 才能形成读/写重叠。
    queue_depth: int = 4
    #: 并行处理的 op 数。HDD 建议 1，SSD 可到 4。
    concurrency: int = 2
    #: 单个 chunk 的最大重试次数（不含首次）。
    max_retries: int = 3
    #: 重试退避基数（毫秒），实际延迟 = base * 2^attempt，带抖动。
    retry_base_ms: int = 50
    #: 退避上限（毫秒）。
    retry_max_ms: int = 2000
    #: 限速（MiB/s），0 表示不限速。
    rate_limit_mbps: float = 0.0
    #: 搬完是否回读校验。
    verify: bool = True
    #: 每完成多少个 chunk 落一次断点。0 表示只在 op 边界落。
    checkpoint_every_chunks: int = 64
    #: 允许对「已挂载的原始卷」写入。默认关闭，打开需要显式确认。
    allow_raw_write_on_mounted: bool = False
    #: 保留卷尾块数（relocate_tail 用）。
    reserve_tail_blocks: int = 0
    #: 「可用空间整理」要腾出的**连续空闲块数**。0 表示没设置 —— 规划器会
    #: 直接判不可行并提示，而不是猜一个默认值然后把盘搬了。
    reclaim_blocks: int = 0
    #: 腾到卷首还是卷尾。
    reclaim_at: FreeSpaceSide = FreeSpaceSide.TAIL
    #: 目标区撞上搬不动的占用（系统元文件、pagefile 等）时，是否把整段
    #: 目标区让开它们。关闭时一律判不可行。
    #:
    #: 打开是默认行为，因为卷首 1 GiB 内几乎必然躺着 ``$MFT`` / ``$LogFile``，
    #: 不让开的话「整理到卷首」这个功能永远只报错。让开之后要说清楚实际
    #: 落在哪个区间（``Plan.reclaim_extent``），不能让用户以为贴着头。
    reclaim_avoid_protected: bool = True

    def __post_init__(self) -> None:
        errors: list[str] = []
        if not (512 <= self.block_size <= 65536) or self.block_size & (self.block_size - 1):
            errors.append("block_size 必须是 512..65536 之间的 2 的幂")
        if not (1 <= self.chunk_blocks <= 65536):
            errors.append("chunk_blocks 必须在 1..65536")
        if not (1 <= self.queue_depth <= 64):
            errors.append("queue_depth 必须在 1..64")
        if not (1 <= self.concurrency <= 16):
            errors.append("concurrency 必须在 1..16")
        if not (0 <= self.max_retries <= 20):
            errors.append("max_retries 必须在 0..20")
        if self.retry_base_ms < 0 or self.retry_max_ms < self.retry_base_ms:
            errors.append("退避参数非法：需 0 <= base <= max")
        if self.rate_limit_mbps < 0:
            errors.append("rate_limit_mbps 不能为负")
        if self.checkpoint_every_chunks < 0:
            errors.append("checkpoint_every_chunks 不能为负")
        if self.reserve_tail_blocks < 0:
            errors.append("reserve_tail_blocks 不能为负")
        if self.reclaim_blocks < 0:
            errors.append("reclaim_blocks 不能为负")
        if not isinstance(self.reclaim_at, FreeSpaceSide):
            errors.append("reclaim_at 必须是 FreeSpaceSide")
        if errors:
            raise ValueError("; ".join(errors))

    @property
    def reclaim_bytes(self) -> int:
        return self.reclaim_blocks * self.block_size

    @property
    def chunk_bytes(self) -> int:
        return self.block_size * self.chunk_blocks

    @property
    def buffer_budget_bytes(self) -> int:
        """流水线的总内存上界（字节），不含 Python 对象开销。"""
        return self.chunk_bytes * self.queue_depth * self.concurrency

    def with_overrides(self, **kw: Any) -> "JobParams":
        return replace(self, **kw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_size": self.block_size,
            "chunk_blocks": self.chunk_blocks,
            "queue_depth": self.queue_depth,
            "concurrency": self.concurrency,
            "max_retries": self.max_retries,
            "retry_base_ms": self.retry_base_ms,
            "retry_max_ms": self.retry_max_ms,
            "rate_limit_mbps": self.rate_limit_mbps,
            "verify": self.verify,
            "checkpoint_every_chunks": self.checkpoint_every_chunks,
            "allow_raw_write_on_mounted": self.allow_raw_write_on_mounted,
            "reserve_tail_blocks": self.reserve_tail_blocks,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "JobParams":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass(frozen=True, slots=True)
class JobSpec:
    """一次任务的输入：对谁、用什么策略、动哪些文件、什么参数。"""

    volume: VolumeInfo
    strategy: Strategy
    params: JobParams = field(default_factory=JobParams)
    file_ids: tuple[str, ...] = ()
    job_id: str = ""

    def __post_init__(self) -> None:
        if self.strategy.writes_blocks and not self.volume.writable:
            raise ValueError(f"卷 {self.volume.mount_point} 不可写，不能执行写策略")


@dataclass(frozen=True, slots=True)
class Progress:
    """进度快照。UI 每次收到就整体替换，不做增量合并——避免状态漂移。"""

    done_blocks: int = 0
    total_blocks: int = 0
    done_ops: int = 0
    total_ops: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    rate_bps: float = 0.0
    eta_s: float | None = None
    retried_chunks: int = 0
    failed_chunks: int = 0
    skipped_ops: int = 0
    current_path: str = ""
    elapsed_s: float = 0.0
    #: 扫描阶段已经看过的条目数。扫描没有"总量"可算（要遍历完才知道有多少
    #: 文件），所以它是**不确定进度**：UI 显示条目数 + 滚动条，而不是百分比。
    #: 执行阶段它保持 0，不要拿它当"已处理文件数"用。
    scanned_items: int = 0

    @property
    def ratio(self) -> float:
        if self.total_blocks <= 0:
            return 0.0
        return min(1.0, self.done_blocks / self.total_blocks)

    @property
    def percent(self) -> float:
        return self.ratio * 100.0

    @property
    def is_complete(self) -> bool:
        return self.total_blocks > 0 and self.done_blocks >= self.total_blocks
