"""保护层：决定"哪些东西不许动"，以及"动了之后还有没有余地"。

这层的存在不是因为"实现不了"，而是因为**搬错一个文件 = 用户丢数据**。
上一代程序把判定散在各个 worker 里，结果是"这个入口拦了，那个入口没拦"。
现在只有一处 :class:`ProtectionPolicy`，规划与执行**各收敛一次**：

* :meth:`ProtectionPolicy.screen` —— 规划前过滤，被拦下的文件根本不会进计划；
* :func:`audit_plan` —— 执行前复核，任何漏网之鱼都会让整份计划被拒绝。

两道闸门都做，是因为"规划时拦住了"依赖调用方记得过滤。少一处就是漏洞。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contract.models import FileFrag, FragReport, Plan, VolumeInfo
from ..ntfs.record import FILE_ATTR_SYSTEM

__all__ = [
    "ProtectionPolicy",
    "PlanAudit",
    "audit_plan",
    "DEFAULT_POLICY",
    "PROTECTED_FRNS",
    "PROTECTED_NAMES",
    "PROTECTED_PATHS",
]

#: MFT 记录 0..15 是元文件（$MFT/$LogFile/$Bitmap/$Boot…），位置被系统写死
PROTECTED_FRNS: frozenset[int] = frozenset(range(16))

#: 名字命中即拦。这些文件要么被系统长期独占打开，要么搬走会开不了机。
PROTECTED_NAMES: frozenset[str] = frozenset(
    {
        "pagefile.sys",
        "hiberfil.sys",
        "swapfile.sys",
        "dumpstack.log.tmp",
        "bootmgr",
        "bootnxt",
        "ntldr",
        "ntdetect.com",
        "boot.ini",
        "bcd",
        "memory.dmp",
    }
)

#: 目录前缀命中即拦（统一小写、反斜杠、无前导分隔符）
PROTECTED_PATHS: tuple[str, ...] = (
    "windows\\winsxs",
    "windows\\system32",
    "windows\\syswow64",
    "windows\\servicing",
    "windows\\installer",
    "windows\\assembly",
    "windows\\boot",
    "windows\\fonts",
    "$recycle.bin",
    "system volume information",
    "$winreagent",
    "recovery",
    "programdata\\microsoft\\windows defender",
    "programdata\\microsoft\\windows\\system",
)


def _norm(path: str) -> str:
    p = path.replace("/", "\\").strip()
    while p.startswith("\\"):
        p = p[1:]
    # 去掉盘符前缀，便于统一比对
    if len(p) > 2 and p[1] == ":":
        p = p[2:].lstrip("\\")
    return p.lower()


@dataclass(frozen=True, slots=True)
class ProtectionPolicy:
    """可配置的保护规则。默认值就是"别碰系统"的保守取值。"""

    protected_frns: frozenset[int] = PROTECTED_FRNS
    protected_names: frozenset[str] = PROTECTED_NAMES
    protected_paths: tuple[str, ...] = PROTECTED_PATHS
    #: 带 FILE_ATTRIBUTE_SYSTEM 的文件一律不动（驱动、系统组件多为这类）
    protect_system_attr: bool = True
    #: 隐藏文件不动（用户自己的隐藏文件不该被"顺手整理"）
    protect_hidden_attr: bool = False
    #: 搬完后至少要留多少比例的空闲（防止把盘整理到没有回旋余地）
    min_free_ratio: float = 0.05
    #: 绝对下限（块）
    min_free_blocks: int = 4096
    #: 系统卷整体禁止写入。默认关：整理 C: 是这类工具的**主要**用途，
    #: 一刀切禁掉等于把产品废了。真正的闸门是界面上的手输盘符确认；
    #: 企业/无人值守场景可以打开它做"绝不碰系统卷"的强约束。
    block_system_volume: bool = False

    # -- 单文件判定 -------------------------------------------------------

    def verdict(self, file: FileFrag) -> str | None:
        """返回拦截原因；``None`` 表示可以动。"""
        try:
            frn = int(file.file_id)
        except (TypeError, ValueError):
            frn = -1
        if frn in self.protected_frns:
            return "系统元文件"
        if self.protect_system_attr and (file.attributes & FILE_ATTR_SYSTEM):
            return "系统属性文件"
        if self.protect_hidden_attr and (file.attributes & 0x0002):
            return "隐藏文件"

        name = _norm(file.path).rsplit("\\", 1)[-1]
        if name in self.protected_names:
            return f"受保护文件名（{name}）"

        norm = _norm(file.path)
        for prefix in self.protected_paths:
            if norm == prefix or norm.startswith(prefix + "\\"):
                return f"受保护目录（{prefix}）"
        return None

    def screen(self, files: tuple[FileFrag, ...]) -> tuple[tuple[FileFrag, ...], list[tuple[FileFrag, str]]]:
        """把文件分成「可动」「拦截」两组。"""
        allowed: list[FileFrag] = []
        blocked: list[tuple[FileFrag, str]] = []
        for f in files:
            why = self.verdict(f)
            if why is None:
                allowed.append(f)
            else:
                blocked.append((f, why))
        return tuple(allowed), blocked

    # -- 卷判定 -----------------------------------------------------------

    def volume_verdict(self, volume: VolumeInfo) -> str | None:
        if self.block_system_volume and volume.is_system:
            return "目标是系统卷，默认禁止写入（需在界面上显式解锁）"
        return None


@dataclass(frozen=True, slots=True)
class PlanAudit:
    """执行前审计结果。"""

    allowed: bool
    reasons: tuple[str, ...] = ()
    blockers: tuple[tuple[str, str], ...] = ()  # (文件路径, 原因)
    free_after_blocks: int = 0
    required_free_blocks: int = 0
    notes: tuple[str, ...] = ()

    def summary(self) -> str:
        if self.allowed:
            return f"审计通过，执行后剩余 {self.free_after_blocks} 块空闲"
        return "审计未通过：" + "；".join(self.reasons)



#: 默认策略实例。模块级单例，避免每个调用点各造一份、参数还不一致。
#: 必须定义在 :func:`audit_plan` 之前——它被当作默认值求值。
DEFAULT_POLICY = ProtectionPolicy()


def audit_plan(
    plan: Plan,
    report: FragReport,
    policy: ProtectionPolicy | None = None,
    *,
    volume: VolumeInfo | None = None,
) -> PlanAudit:
    """执行前复核：计划里有没有受保护对象、搬完还剩多少空间。

    返回 ``allowed=False`` 时调用方必须拒绝执行——**不允许**"跳过有问题的
    op 继续跑"，因为计划是一个整体，少搬一个文件会让后续 op 的腾挪前提失效。
    """
    policy = policy or DEFAULT_POLICY
    reasons: list[str] = []
    blockers: list[tuple[str, str]] = []

    if volume is not None:
        v = policy.volume_verdict(volume)
        if v:
            reasons.append(v)

    by_id = {f.file_id: f for f in report.files}
    for op in plan.ops:
        target = by_id.get(op.file_id)
        if target is None:
            # 计划里的文件在报告里找不到 = 计划过期，不能凭它写盘
            blockers.append((op.path, "文件不在当前扫描结果中（计划已过期）"))
            continue
        why = policy.verdict(target)
        if why:
            blockers.append((op.path, why))

    if blockers:
        reasons.append(f"{len(blockers)} 个受保护或已失效的文件出现在计划中")

    free_after = report.free_blocks - plan.total_blocks
    # 绝对下限不能压过比例下限：小卷（测试用的 1024 块合成盘）上"至少留
    # 4096 块"是不可能满足的，一刀切会把所有小卷判死。取两者中较小者兜底，
    # 再用比例保证大卷一定有足够余量。
    ratio_floor = int(report.geometry.block_count * policy.min_free_ratio)
    absolute_floor = min(policy.min_free_blocks, max(1, report.geometry.block_count // 20))
    required = max(ratio_floor, absolute_floor)
    if free_after < required:
        reasons.append(
            f"执行后仅剩 {free_after} 块空闲，低于安全下限 {required} 块"
        )

    return PlanAudit(
        allowed=not reasons,
        reasons=tuple(reasons),
        blockers=tuple(blockers[:20]),
        free_after_blocks=free_after,
        required_free_blocks=required,
    )

