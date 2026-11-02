"""失败诊断：把"出错了"变成一段能直接贴给 AI 或贴进 issue 的文字。

这个模块是为一个具体场景写的：**用户只能靠描述现象求助**。

旧版日志写一行 ``op12 失败：R:\\a.pst — 拒绝访问``，用户转述给别人时只能
说"它报错了，说拒绝访问"。对方（人或 AI）手里没有版本、没有卷信息、不知道
错在哪一步、不知道 Windows 到底返回了什么错误码 —— 于是第一轮对话必然是
在追问这些。这里把该问的全都预先写出来。

设计上分三层，缺一层都会让求助变得更难：

1. **单条失败**（:func:`format_failure`）：阶段 / 原因 / 异常类型 / 系统错误码
   / 一句可执行的建议。前四项是"定位"，最后一项是"我现在能做什么"。
2. **整体报告**（:func:`build_report`）：环境、目标卷、策略参数、结果统计，
   加上失败明细与最近日志。这些是复现的前提条件，缺一样对方就只能猜。
3. **建议映射**（:func:`hint_for`）：把最常见的几个系统错误码翻译成动作。
   没有它，``winerror=32`` 对普通用户等于没说。

刻意不做的事：**不猜原因，也不建议"重试"**。重试是最没信息量的建议 ——
如果是权限问题，重试一百次也一样。
"""

from __future__ import annotations

import os
import platform
import sys
from typing import Iterable, Sequence

from .contract.models import FailureDetail, JobParams, VolumeInfo

__all__ = [
    "hint_for",
    "format_failure",
    "build_report",
    "environment_lines",
    "WINERROR_HINTS",
]

#: 常见 Windows 错误码 → 该怎么办。
#:
#: 只收"真的会遇到的"这几种：用户要的是下一步动作，不是一本错误码手册。
WINERROR_HINTS: dict[int, str] = {
    5: "权限不足：确认以管理员身份运行；系统目录/受保护文件本就不允许搬",
    19: "介质写保护：这块盘是只读的，检查物理写保护开关或磁盘策略",
    32: "文件被其它进程占用：关掉正在使用它的程序（资源监视器 → 磁盘 可查占用进程）后重试",
    33: "文件被锁定了一部分：同上，先关掉占用程序",
    87: "参数不正确：多半是簇大小/卷几何与实际不符，请把这段报告发出来",
    112: "磁盘空间不足：目标卷没有足够的连续空闲，先把「保留卷尾块数」调小或清理空间",
    1224: "文件被内存映射（正在运行的程序/数据库文件）：关掉对应程序再试",
    1392: "文件或目录已损坏：先对该卷做一次 chkdsk",
    1450: "系统资源不足：关掉一些程序后重试",
    433: "设备不识别该请求：该卷不是 NTFS，或不是本地磁盘",
    1005: "卷已被锁定：另一个程序（含本程序的另一次运行）正独占该卷",
    1117: "设备 I/O 错误：多为硬件/连接问题，先跑一次 chkdsk 并检查数据线",
    21: "设备未就绪：盘可能已拔出或休眠，重新连接后再试",
}

#: 按阶段给的通用建议（错误码没命中时兜底）。
_STAGE_HINTS: dict[str, str] = {
    "打开文件": "该文件当前打不开：检查是否被占用、路径是否过长、是否有权限",
    "复核布局": (
        "扫描之后这个文件的簇布局变了：下载/做种、播放器、杀毒扫描或数据库写入都可能"
        "改它。重新扫描一次再开始即可（本次已跳过它）；同一个文件反复出现就先关掉正在"
        "写它的程序"
    ),
    "搬簇": "文件系统拒绝搬这一段：先跑一次 chkdsk，再看是否空间不足",
    "读取数据": "源数据读不出来：可能是坏块。建议先备份该文件并做磁盘检查",
    "写入数据": "目标位置写不进去：检查卷是否只读、是否已满、是否有坏道",
    "回读校验": "写进去的数据读回来不一致：可能是坏块或缓存问题，建议先 chkdsk",
}


def hint_for(failure: FailureDetail) -> str:
    """给一条**可执行**的建议。错误码优先，其次看阶段。"""
    if failure.winerror and failure.winerror in WINERROR_HINTS:
        return WINERROR_HINTS[failure.winerror]
    if failure.error_class == "cancelled":
        return "任务被停止，不是错误"
    if failure.error_class == "verify_mismatch":
        return "回读校验不一致：可以先重试一次；连续出现说明该位置有坏道"
    if failure.stage in _STAGE_HINTS:
        return _STAGE_HINTS[failure.stage]
    return "把这段报告发出来，可以据此定位（含版本、阶段、异常类型与错误码）"


def format_failure(index: int, failure: FailureDetail, *, with_hint: bool = True) -> str:
    """单条失败的多行描述。缩进而不是一行到底，是为了贴进聊天窗口还可读。"""
    lines = [
        f"{index}. {failure.path}",
        f"   阶段：{failure.stage or '未知'}",
        f"   原因：{failure.reason or '（无描述）'}",
    ]
    meta = []
    if failure.error_type:
        meta.append(f"类型={failure.error_type}")
    if failure.error_class:
        meta.append(f"分类={failure.error_class_label}")
    if failure.winerror:
        meta.append(f"winerror={failure.winerror}")
    if meta:
        lines.append("   " + "  ".join(meta))
    if with_hint:
        lines.append(f"   建议：{hint_for(failure)}")
    return "\n".join(lines)


def _fmt_bytes(n: int) -> str:
    """人类可读的容量。

    刻意不 import ``ui.format``：诊断信息要能在 CLI、冒烟脚本、无 GUI 的
    环境里生成，而 ``ui`` 那一层的存在本身就意味着 PySide6。
    """
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} TiB"


def environment_lines() -> list[str]:
    """环境信息。**这些是复现的前提**，缺一个都可能让排查走偏。"""
    from . import VERSION_DISPLAY

    admin = "?"
    if os.name == "nt":
        try:
            from .platform.privilege import is_admin

            admin = "是" if is_admin() else "否"
        except Exception:  # noqa: BLE001 - 探测失败不该影响诊断
            admin = "?"
    return [
        f"版本: LBAMover {VERSION_DISPLAY}",
        f"系统: {platform.platform()}",
        f"Python: {sys.version.split()[0]} ({platform.machine()})",
        f"管理员: {admin}",
    ]


def build_report(
    *,
    volume: VolumeInfo | None = None,
    strategy: str = "",
    scope: str = "",
    params: JobParams | None = None,
    moved_ops: int = 0,
    failed_ops: int = 0,
    skipped_ops: int = 0,
    elapsed_s: float = 0.0,
    aborted: str = "",
    failures: Sequence[FailureDetail] = (),
    log_tail: Iterable[str] = (),
    max_failures: int = 20,
) -> str:
    """拼一段可以直接复制走的诊断报告。

    顺序是刻意的：先说"这是什么环境"（对方判断能不能复现），再说"在什么
    盘上做什么"（判断风险面），然后"结果如何"，最后才是明细与日志 ——
    倒过来写的话最有用的信息会被埋在几百行日志下面。
    """
    out: list[str] = ["=== LBAMover 诊断报告 ==="]
    out.extend(environment_lines())

    if volume is not None:
        geo = volume.geometry
        out.append(
            f"目标卷: {volume.mount_point} [{volume.filesystem}] "
            f"{_fmt_bytes(geo.total_bytes)} 块大小 {geo.block_size} 字节 "
            f"{'系统卷' if volume.is_system else '数据卷'}"
        )
    if strategy:
        out.append(f"策略: {strategy}")
    if scope:
        out.append(f"范围: {scope}")
    if params is not None:
        # 这两项决定"该搬多少个文件"。报告里没有它们，就分不清"预演说 4 万个、
        # 实际只动了 200 个"是引擎出错还是参数选错了 —— 这是用户最常问的那类
        # 问题，答案只能在这里。
        notes = ""
        if params.intensity.value == "extreme":
            notes += f"（大文件下限 {params.extreme_min_mib} MiB）"
        span = str(getattr(strategy, "value", strategy))
        if span == "pack_tail":
            how_much = (
                "整卷"
                if params.pack_span_blocks <= 0
                else f"{params.pack_span_blocks * params.block_size / 1024**3:.1f} GiB"
            )
            notes += f"，压实范围={how_much}"
        out.append(
            f"参数: chunk={params.chunk_blocks} 块, 队列={params.queue_depth}, "
            f"并发={params.concurrency}, 校验={'开' if params.verify else '关'}, "
            f"重试={params.max_retries}, 限速={params.rate_limit_mbps:g} MiB/s, "
            f"忽略错误={'开' if params.ignore_errors else '关'}, "
            f"强度={params.intensity.label}{notes}, "
            # 搬运方式与缓存目录必须在一起报：切到 cached 又没生效时，
            # 用户唯一能看出问题的地方就是这两项的搭配。
            f"搬运={params.exec_mode.value}"
            + (f"（{params.cache_dir}）" if params.exec_mode.value == "cached" else "")
            + f", 预占={'开' if params.preoccupy_target else '关'}"
        )

    out.append(
        f"结果: 完成 {moved_ops} / 失败 {failed_ops} / 跳过 {skipped_ops}，"
        f"耗时 {elapsed_s:.1f}s"
    )
    if aborted:
        out.append(f"中止: {aborted}")

    if failures:
        out.append("")
        out.append(f"失败明细（共 {len(failures)} 个，最多列 {max_failures} 个）：")
        for i, item in enumerate(list(failures)[:max_failures], 1):
            out.append(format_failure(i, item))
        if len(failures) > max_failures:
            out.append(f"…… 另有 {len(failures) - max_failures} 个同类失败未列出")

    tail = [line for line in log_tail if line.strip()]
    if tail:
        out.append("")
        out.append("最近日志：")
        out.extend(f"  {line}" for line in tail)

    out.append("")
    out.append("=== 报告结束（把以上全部内容贴出来即可）===")
    return "\n".join(out)
