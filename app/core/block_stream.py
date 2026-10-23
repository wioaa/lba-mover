"""用户态「读 → 写暂存盘 → 再读出」分块流式搬迁内核（1.0.0C 特殊工作模式）。

与 ``FSCTL_MOVE_FILE``（簇级、元数据操作、需管理卷特权）不同，本模式完全在
**用户态**用普通 ``ReadFile`` / ``WriteFile`` 按块搬数据：

* **block 固定 65536 簇**（:data:`BLOCK_CLUSTERS`）；
* **可调缓存按 block 的倍数积攒**（``buffer_blocks`` = 1×/2×/…，见 :class:`BlockBuffer`）；
* **双阶段流水线**：``load``（源 → 暂存盘）与 ``store``（暂存盘 → 目的）重叠推进，
  一个分块走了立马顶上下一块，缓解 HDD 半双工；数据经文件系统落地，源簇被读出后
  逻辑上即刻被重新占用，因而不存在严格的落点竞争。

本模块只含**纯逻辑**（分块计划 / 可调缓存 / 流水线调度），可离线单测；
真正的 Win32 文件读写在 ``app.win32.file_io``（scaffolding，**需真机验证**）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

#: 块大小（簇）。按需求固定为 65536 簇。
BLOCK_CLUSTERS = 65536

#: 可调缓存默认倍数（缓冲容纳多少个 block）。
DEFAULT_BUFFER_BLOCKS = 2


@dataclass(frozen=True)
class Block:
    """一个待搬运的数据块（源文件内的一段）。"""

    index: int
    offset: int   # 源文件内字节偏移
    length: int   # 本块字节数（末块可不足一个整块）


def plan_blocks(
    total_bytes: int,
    bytes_per_cluster: int,
    block_clusters: int = BLOCK_CLUSTERS,
) -> List[Block]:
    """把文件按 ``block_clusters`` 切成块序列（末块为残长）。

    Args:
        total_bytes: 文件字节数。
        bytes_per_cluster: 每簇字节数。
        block_clusters: 块大小（簇），默认 :data:`BLOCK_CLUSTERS`。

    Returns:
        :class:`Block` 列表；``total_bytes <= 0`` 或参数非法时返回空表。
    """
    if total_bytes <= 0 or bytes_per_cluster <= 0 or block_clusters <= 0:
        return []
    block_bytes = block_clusters * bytes_per_cluster
    count = (total_bytes + block_bytes - 1) // block_bytes
    blocks: List[Block] = []
    for i in range(count):
        off = i * block_bytes
        blocks.append(Block(index=i, offset=off, length=min(block_bytes, total_bytes - off)))
    return blocks


class BlockBuffer:
    """可调缓存：容纳 ``buffer_blocks`` 个 block（即 block 大小的整数倍）。

    ``buffer_blocks`` 可调（1 / 2 / 3 …），缓存容量 = ``buffer_blocks × block_clusters``
    簇；流水线深度即 ``buffer_blocks``——越大读写重叠越多、加速越明显，占用也越大。
    """

    def __init__(
        self,
        buffer_blocks: int = DEFAULT_BUFFER_BLOCKS,
        block_clusters: int = BLOCK_CLUSTERS,
        bytes_per_cluster: int = 4096,
    ) -> None:
        if int(buffer_blocks) < 1:
            raise ValueError(f"缓冲倍数须 ≥ 1（block 的整数倍），得到 {buffer_blocks}")
        if block_clusters <= 0 or bytes_per_cluster <= 0:
            raise ValueError("block_clusters / bytes_per_cluster 须为正")
        self.buffer_blocks = int(buffer_blocks)
        self.block_clusters = int(block_clusters)
        self.bytes_per_cluster = int(bytes_per_cluster)

    @property
    def capacity_clusters(self) -> int:
        """缓存容量（簇）——``buffer_blocks`` 个 block。"""
        return self.buffer_blocks * self.block_clusters

    @property
    def capacity_bytes(self) -> int:
        """缓存容量（字节）。"""
        return self.capacity_clusters * self.bytes_per_cluster


def stream_schedule(num_blocks: int, buffer_blocks: int) -> List[Tuple[str, int]]:
    """生成双阶段流水线调度。

    每个动作是 ``("load", i)``（读源块 i → 写暂存盘）或 ``("store", i)``
    （读暂存块 i → 写目的）。调度保证：

    * 每块的 ``store`` 必在其 ``load`` 之后；
    * 暂存缓冲**同时驻留块数 ≤ ``buffer_blocks``**（FIFO，先进先出）；
    * ``buffer_blocks ≥ 2`` 时 ``load`` 与 ``store`` 重叠推进（流水加速）；
      ``buffer_blocks == 1`` 时退化为严格的 load→store→load→store 串行。

    Raises:
        ValueError: ``buffer_blocks < 1``。
    """
    if buffer_blocks < 1:
        raise ValueError("buffer_blocks 须 ≥ 1")
    ops: List[Tuple[str, int]] = []
    load_next = 0        # 下一块待 load 的索引
    store_next = 0       # 下一块待 store 的索引（FIFO）
    resident = 0         # 当前驻留暂存盘的块数
    while store_next < num_blocks:
        # 缓冲未满就继续 load（积攒到 buffer_blocks 个 block）
        while resident < buffer_blocks and load_next < num_blocks:
            ops.append(("load", load_next))
            resident += 1
            load_next += 1
        # 缓冲非空就 store 最早加载的一块（腾出位置让后续 load 顶上）
        if resident > 0:
            ops.append(("store", store_next))
            resident -= 1
            store_next += 1
    return ops


def simulate_peak_resident(ops: List[Tuple[str, int]]) -> int:
    """回放任一调度的暂存驻留峰值（块数）——供测试校验缓冲上界。"""
    resident = 0
    peak = 0
    for kind, _index in ops:
        if kind == "load":
            resident += 1
            peak = max(peak, resident)
        elif kind == "store":
            resident -= 1
    return peak


def has_pipeline_overlap(ops: List[Tuple[str, int]]) -> bool:
    """调度是否存在 load/store 重叠（某次 load 发生时，尚有更早的块未 store）。"""
    loaded: List[int] = []
    stored: set = set()
    for kind, index in ops:
        if kind == "load":
            # 若此刻还有已 load 未 store 的块，说明 load 与在途 store 重叠
            if any(i not in stored for i in loaded):
                return True
            loaded.append(index)
        else:  # store
            stored.add(index)
    return False
