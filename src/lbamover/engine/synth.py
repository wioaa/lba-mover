"""合成卷：造一个"确定性的、可校验的"碎片场景。

存在的理由：碎片整理的核心风险是**数据被搬错**，而"看起来跑通了"完全
不能证明数据没错。这里生成的每个块内容都是 ``(seed, 文件, 块序号)`` 的
哈希，搬完之后逐块比对，才能说"数据一致"。

同一份 layout 在任何机器、任何次数下生成的内容完全相同，因此可以进
单元测试当基准。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

from ..contract.models import Extent, FileFrag, Geometry
from ..devices.base import BlockDeviceBase
from .frag import free_extents_from_files

__all__ = [
    "SynthLayout",
    "apply_plan",
    "build_layout",
    "populate",
    "verify",
    "block_payload",
]

_FILL_CACHE: dict[tuple[int, str, int, int], bytes] = {}


def block_payload(seed: int, file_id: str, index: int, block_size: int) -> bytes:
    """某文件第 ``index`` 块的确定性内容（带缓存，避免重复哈希）。"""
    key = (seed, file_id, index, block_size)
    cached = _FILL_CACHE.get(key)
    if cached is not None:
        return cached
    digest = hashlib.blake2b(
        f"{seed}:{file_id}:{index}".encode("utf-8"), digest_size=32
    ).digest()
    reps = (block_size + len(digest) - 1) // len(digest)
    data = (digest * reps)[:block_size]
    if len(_FILL_CACHE) < 20000:
        _FILL_CACHE[key] = data
    return data


@dataclass(frozen=True, slots=True)
class SynthLayout:
    geometry: Geometry
    files: tuple[FileFrag, ...]
    free: tuple[Extent, ...]
    seed: int = 0

    @property
    def total_blocks(self) -> int:
        return self.geometry.block_count

    @property
    def fragmented_files(self) -> tuple[FileFrag, ...]:
        return tuple(f for f in self.files if f.is_fragmented)


def build_layout(
    total_blocks: int,
    *,
    file_count: int = 8,
    block_size: int = 4096,
    seed: int = 7,
    max_runs: int = 4,
    max_run_blocks: int = 6,
) -> SynthLayout:
    """生成一个碎片化布局。

    每个文件被切成 1..``max_runs`` 段，段之间留 0..3 块的缝隙，从而同时
    制造"文件碎片"和"自由空间碎片"两种场景。
    """
    if total_blocks <= 0:
        raise ValueError("total_blocks 必须为正")
    rnd = random.Random(seed)
    files: list[FileFrag] = []
    cursor = 0

    for i in range(file_count):
        runs = rnd.randint(1, max(1, max_runs))
        extents: list[Extent] = []
        for _ in range(runs):
            gap = rnd.randint(0, 3)
            length = rnd.randint(1, max(1, max_run_blocks))
            start = cursor + gap
            if start + length > total_blocks:
                break
            extents.append(Extent(start, length))
            cursor = start + length
        if not extents:
            break
        files.append(
            FileFrag(
                file_id=f"f{i:03d}",
                path=f"C\\data\\file_{i:03d}.bin",
                extents=tuple(extents),
            )
        )

    geo = Geometry(block_size=block_size, block_count=total_blocks)
    free = free_extents_from_files(geo, files)
    return SynthLayout(geometry=geo, files=tuple(files), free=free, seed=seed)


def populate(device: BlockDeviceBase, layout: SynthLayout, seed: int | None = None) -> None:
    """把合成数据写进设备。每个文件的逻辑内容按 extent 顺序拼接。"""
    s = layout.seed if seed is None else seed
    bs = layout.geometry.block_size
    for f in layout.files:
        logical = 0
        for ext in f.extents:
            for k in range(ext.length):
                device.write_blocks(ext.start + k, block_payload(s, f.file_id, logical + k, bs))
            logical += ext.length
    device.flush()


def apply_plan(layout: SynthLayout, plan) -> SynthLayout:
    """把计划「执行后的布局」算出来，用于验证搬迁结果。

    这一步是必需的：搬完之后旧位置上的数据已经作废，继续按原始 layout 校验
    只会得到一片假失败。真实校验的对象必须是**新布局**。
    """
    moved = {op.file_id: op for op in plan.ops}
    new_files: list[FileFrag] = []
    for f in layout.files:
        op = moved.get(f.file_id)
        if op is None:
            new_files.append(f)
            continue
        new_files.append(
            FileFrag(
                file_id=f.file_id,
                path=f.path,
                extents=(Extent(op.dst_start, op.blocks),),
                attributes=f.attributes,
            )
        )
    return SynthLayout(
        geometry=layout.geometry,
        files=tuple(new_files),
        free=free_extents_from_files(layout.geometry, new_files),
        seed=layout.seed,
    )


def verify(device: BlockDeviceBase, layout: SynthLayout, seed: int | None = None) -> list[str]:
    """逐块校验。返回问题列表，空表示完全一致。"""
    s = layout.seed if seed is None else seed
    bs = layout.geometry.block_size
    problems: list[str] = []
    for f in layout.files:
        logical = 0
        for ext in f.extents:
            for k in range(ext.length):
                want = block_payload(s, f.file_id, logical + k, bs)
                got = device.read_blocks(ext.start + k, 1)
                if got != want:
                    problems.append(
                        f"{f.path}: LBA {ext.start + k} 内容不一致"
                        f"（期望块序号 {logical + k}）"
                    )
                    if len(problems) > 20:
                        problems.append("...（已截断）")
                        return problems
            logical += ext.length
    return problems
