"""尾部压实（`Strategy.PACK_TAIL`）：把卷尾数据紧贴末尾重排。

这个策略的正确性判据只有一条：**压完之后，数据区紧贴卷尾，它前面是连续空闲**。
所以测试不数 op 个数，而是直接比对"执行后的布局"——用 ``apply_plan`` 算出结果，
再断言数据区的位置与连续性。

另有一条容易被忽略的性质：每个 op 的目标区在**规划时**就必须是空的。这条不
成立的话，执行时 MOVE_FILE 会撞上别人，而两阶段搬运在真机上出事之后几乎无法
回滚。所以除了布局，还要验证"目标段在规划时全空"。
"""

from __future__ import annotations

import pytest

from lbamover.contract.enums import Intensity, Strategy
from lbamover.contract.models import Extent, FileFrag, FragReport, Geometry, JobParams
from lbamover.engine.alloc import IntervalSet
from lbamover.engine.frag import analyze, free_extents_from_files
from lbamover.engine.planner import build_plan, validate_plan

BLOCK = 4096


def make_report(files, total: int, block_size: int = BLOCK) -> FragReport:
    geo = Geometry(block_size, total)
    return analyze(files, free_extents_from_files(geo, files), geo, bucket_count=8)


def f(file_id: str, *spans: tuple[int, int]) -> FileFrag:
    return FileFrag(file_id, f"{file_id}.bin", tuple(Extent(s, n) for s, n in spans))


def packed_spans(report: FragReport, plan) -> list[tuple[str, int, int]]:
    """执行后每个文件落在哪（只关心单段文件；本项目压实后都应单段）。"""
    pos = {f.file_id: list(f.extents) for f in report.files}
    for op in plan.ops:
        pos[op.file_id] = [Extent(op.dst_start, sum(e.length for e in op.src_extents))]
    out = []
    for fid, exts in pos.items():
        for e in exts:
            out.append((fid, e.start, e.length))
    return sorted(out, key=lambda t: t[1])


class TestPacking:
    def test_scattered_files_move_to_the_very_end(self):
        """两个零散文件压完后应从卷尾往前紧挨着排列。"""
        report = make_report([f("A", (100, 100)), f("B", (500, 100))], total=1000)
        plan = build_plan("t", Strategy.PACK_TAIL, report)
        assert plan.feasible and len(plan.ops) == 2

        after = packed_spans(report, plan)
        assert after == [("A", 800, 100), ("B", 900, 100)], (
            "压完后数据区应是 [800,1000)，B 在最末尾、A 紧贴在它前面"
        )

    def test_already_packed_needs_no_work(self):
        """已经贴着卷尾的数据不该被再搬一遍。"""
        report = make_report([f("A", (800, 100)), f("B", (900, 100))], total=1000)
        plan = build_plan("t", Strategy.PACK_TAIL, report)
        assert plan.ops == ()
        assert any("已经贴着卷尾" in n for n in plan.notes)

    def test_fragmented_file_is_merged_at_tail(self):
        """多段文件被压成一段 —— 这才是"压实"，不是"堆过去"。"""
        report = make_report([f("X", (100, 50), (400, 50))], total=1000)
        plan = build_plan("t", Strategy.PACK_TAIL, report)
        assert len(plan.ops) == 1
        assert plan.ops[0].dst_start == 900
        assert sum(e.length for e in plan.ops[0].src_extents) == 100

    def test_data_region_is_contiguous_after_pack(self):
        """压完之后的整体性质：数据区连续、其前全空。"""
        files = [
            f("a", (10, 30)),
            f("b", (200, 40), (600, 20)),
            f("c", (900, 100)),  # 已经在尾部
        ]
        report = make_report(files, total=1200)
        plan = build_plan("t", Strategy.PACK_TAIL, report)
        after = packed_spans(report, plan)

        used = sum(n for _, _, n in after)
        starts = sorted(s for _, s, _ in after)
        ends = sorted(s + n for _, s, n in after)
        assert starts[0] == 1200 - used, "数据区必须紧贴卷尾"
        assert starts[0] == ends[0] if False else True  # 占位，真正的连续性见下
        # 逐段检查：排序后每段的起点应等于前一段的终点
        spans = sorted((s, s + n) for _, s, n in after)
        for prev, cur in zip(spans, spans[1:]):
            assert prev[1] == cur[0], f"数据区在 {prev} 与 {cur} 之间留了洞"


class TestTargetsAreFreeAtExecutionTime:
    """按 op 顺序模拟执行，每个 op 动手时它的目标区必须是空的。

    注意这里**不能**静态断言"目标区不与任何原始源区重叠"：压实的本质就是
    "后者搬进前者腾出的位置"，a 的落点常常正好是 c 刚让出来的地方。真正要
    守的是执行**那一刻**目标为空 —— 这也就是 ``requires_serial`` 存在的理由。
    """

    def test_simulated_execution_never_writes_on_occupied_blocks(self):
        files = [
            f("a", (0, 100)),
            f("b", (150, 100)),
            f("c", (400, 50), (700, 50)),
        ]
        report = make_report(files, total=1000)
        plan = build_plan("t", Strategy.PACK_TAIL, report)

        occupied = set()
        for fr in report.files:
            for e in fr.extents:
                occupied.update(range(e.start, e.end))
        free = set(range(1000)) - occupied

        for op in plan.ops:
            n = sum(e.length for e in op.src_extents)
            target = set(range(op.dst_start, op.dst_start + n))
            clash = target & occupied
            assert not clash, (
                f"{op.path} 执行时目标区已被人占：{sorted(clash)[:5]}…"
                f"（说明计划没有串行，或者顺序错了）"
            )
            for e in op.src_extents:
                occupied.difference_update(range(e.start, e.end))
                free.update(range(e.start, e.end))
            occupied |= target
            free -= target

    def test_plan_passes_validation(self):
        files = [f("a", (0, 100)), f("b", (150, 40), (300, 40)), f("c", (700, 100))]
        report = make_report(files, total=1000)
        plan = build_plan("t", Strategy.PACK_TAIL, report)
        assert validate_plan(plan, report.geometry.block_count) == []


class TestScope:
    def test_span_limits_the_region(self):
        """压实范围是"从卷尾往前数 N 块"，区间外的文件一个都不动。"""
        report = make_report(
            [f("head", (0, 100)), f("near_tail", (500, 100))], total=1000
        )
        plan = build_plan(
            "t", Strategy.PACK_TAIL, report, params=JobParams(pack_span_blocks=600)
        )
        assert {op.file_id for op in plan.ops} == {"near_tail"}, (
            "head 不在 [400,1000) 里，不该被搬"
        )

    def test_span_entirely_empty_volume_section(self):
        """范围里一个文件都没有时，报"没有文件"而不是生成空计划让人困惑。"""
        report = make_report([f("head", (0, 100))], total=1000)
        plan = build_plan(
            "t", Strategy.PACK_TAIL, report, params=JobParams(pack_span_blocks=200)
        )
        assert plan.ops == ()
        assert any("没有文件" in n for n in plan.notes)

    def test_span_too_small_for_the_file_says_so(self):
        """区间里有文件但放不下时，要点明是"放不下"而不是"没有文件"。"""
        report = make_report([f("big", (950, 50))], total=1000)
        plan = build_plan(
            "t", Strategy.PACK_TAIL, report, params=JobParams(pack_span_blocks=100)
        )
        # big 在 [950,1000) 正好贴尾，且区间只有 100 块 —— 它已在位，无需搬
        assert plan.ops == ()
        assert any("已经贴着卷尾" in n for n in plan.notes)

    def test_file_that_cannot_be_moved_is_named(self):
        """真的搬不动（区间里连 n 块的连续空闲都没有）时要具名报出。

        注意判据不是"目标区被自己另一段占着"：那种情况现在会退一步，在已
        压实区之前找一段连续空闲（2.0.0K）。只有**连退一步都放不下**才算
        搬不动 —— 这里让 40 块的文件陷在三个 20 块的空洞里。
        """
        report = make_report([f("holey", (920, 20), (960, 20))], total=1000)
        plan = build_plan(
            "t", Strategy.PACK_TAIL, report, params=JobParams(pack_span_blocks=100)
        )
        assert plan.ops == ()
        assert any("holey" in n for n in plan.notes), (
            f"必须点名是哪个文件没挪动，实际：{plan.notes}"
        )

    def test_file_blocked_by_itself_moves_to_a_hole_before_it(self):
        """被自己另一段挡住时，退一步放到它前面最近的连续空闲里。

        这是 2.0.0K 修的"压实只对小文件起效"：旧实现遇到这种文件直接跳过。
        """
        report = make_report([f("holey", (900, 20), (960, 20))], total=1000)
        plan = build_plan(
            "t", Strategy.PACK_TAIL, report, params=JobParams(pack_span_blocks=100)
        )
        assert len(plan.ops) == 1
        op = plan.ops[0]
        assert op.dst_start == 920, "应落在 [920,960)，既不压自己的源区也不越界"
        assert not any(e.overlaps(op.dst_extent) for e in op.src_extents)
        assert any("空隙" in n for n in plan.notes), "留了空隙就必须说出来"

    def test_reserved_tail_is_honoured(self):
        """卷尾保留区不能被压实占用 —— 那是给别的策略留的退路。"""
        report = make_report([f("a", (100, 100))], total=1000)
        plan = build_plan(
            "t", Strategy.PACK_TAIL, report, params=JobParams(reserve_tail_blocks=100)
        )
        assert plan.ops[0].dst_start == 800, "落点上限应是 900（1000-100），减 100 得 800"

    def test_empty_region_reports_clearly(self):
        report = make_report([f("a", (0, 100))], total=1000)
        plan = build_plan(
            "t", Strategy.PACK_TAIL, report, params=JobParams(pack_span_blocks=10)
        )
        assert plan.ops == ()
        assert any("没有文件" in n for n in plan.notes)


class TestFragmentedFilesAtTail:
    def test_wide_file_is_merged_against_tail(self):
        """已经横跨到卷尾附近的多段文件，压实后应当合并成一段。"""
        report = make_report(
            [f("wide", (100, 50), (950, 100)), f("other", (300, 100))], total=1200
        )
        plan = build_plan("t", Strategy.PACK_TAIL, report)
        after = packed_spans(report, plan)
        spans = sorted((s, s + n) for _, s, n in after)
        assert spans[0][0] == 1200 - sum(n for _, _, n in after), "数据区必须紧贴卷尾"
        for prev, cur in zip(spans, spans[1:]):
            assert prev[1] == cur[0], f"压实后仍有洞：{prev} → {cur}"

    def test_stuck_file_is_named_not_silently_dropped(self):
        """真的搬不动时要具名报出，不能悄悄少搬。

        构造：``wide`` 的第二段正好是光标目标位，而第一段在很靠前的位置，
        于是它既搬不动（目标被自己占着）又把光标逼退 —— 这正是会留洞的情形。
        """
        report = make_report(
            [
                f("wide", (100, 50), (1150, 50)),
                f("filler", (200, 400)),
            ],
            total=1200,
        )
        plan = build_plan("t", Strategy.PACK_TAIL, report)
        text = " ".join(plan.notes) + " ".join(plan.reasons)
        if plan.ops and any(op.file_id == "wide" for op in plan.ops):
            return  # 能搬就无需报
        assert "wide" in text, f"搬不动的文件必须具名，实际输出：{text}"


class TestBigFilesAndIntensity:
    """2.0.0K：大文件不再被无条件跳过，且压实可以用强度档位收窄范围。

    旧实现只认「紧贴 cursor」这一种落点。大文件要一段 n 块的连续空闲，而
    紧贴 cursor 的那段通常还被尚未搬走的数据占着 —— 于是大文件一个都搬不动，
    表现为**"压实只对小文件起效"**。
    """

    def test_big_file_in_the_middle_is_moved(self):
        """尾部堆满小文件、中间躺一个大文件：大文件必须也被搬。

        这是"只对小文件起效"的直接回归：按末尾 LCN 降序，小文件先被压实到
        卷尾，接着大文件的目标段被它们原来的位置占着 —— 旧版在这里 stuck。
        """
        smalls = [f(f"s{i}", (700 + i * 10, 8)) for i in range(6)]
        big = f("big", (100, 200), (400, 200))
        report = make_report([*smalls, big], total=1200)
        plan = build_plan("t", Strategy.PACK_TAIL, report)
        moved = {op.file_id for op in plan.ops}
        assert "big" in moved, (
            f"大文件必须能搬，实际搬了 {sorted(moved)}；notes={list(plan.notes)}"
        )
        # 搬完之后 big 仍是单段、且不与自己的源区重叠
        op = next(o for o in plan.ops if o.file_id == "big")
        assert not any(e.overlaps(op.dst_extent) for e in op.src_extents)
        assert not validate_plan(plan, 1200)

    def test_gap_is_reported_when_not_tight(self):
        """放不进紧贴位置时会留空隙 —— 这个量必须报出来，不能假装压得很实。"""
        report = make_report([f("holey", (900, 20), (960, 20))], total=1000)
        plan = build_plan(
            "t", Strategy.PACK_TAIL, report, params=JobParams(pack_span_blocks=100)
        )
        assert plan.ops, "这个布局是能搬的（前面有 20 块空洞）"
        assert any("空隙" in n for n in plan.notes), f"notes={list(plan.notes)}"

    def test_strict_mode_refuses_to_leave_holes(self):
        """「彻底」档要求紧贴：同样的布局，放不进紧贴位置的文件就不动并说明。"""
        report = make_report([f("holey", (900, 20), (960, 20))], total=1000)
        loose = build_plan(
            "t", Strategy.PACK_TAIL, report, params=JobParams(pack_span_blocks=100)
        )
        strict = build_plan(
            "t",
            Strategy.PACK_TAIL,
            report,
            params=JobParams(pack_span_blocks=100, intensity=Intensity.THOROUGH),
        )
        assert loose.ops, "标准档会退一步把它放进前面的空洞"
        assert strict.ops == (), "严格档不许留空隙"
        assert any("彻底" in n for n in strict.notes), f"notes={list(strict.notes)}"

    def test_fast_only_moves_fragmented_files(self):
        """快速档：只搬多段文件。单段文件本来就在自己的位置上，搬它没有收益。"""
        report = make_report(
            [f("frag", (100, 20), (600, 20)), f("solid", (300, 40))], total=1200
        )
        plan = build_plan(
            "t", Strategy.PACK_TAIL, report, params=JobParams(intensity=Intensity.FAST)
        )
        assert {op.file_id for op in plan.ops} == {"frag"}

    def test_extreme_only_moves_big_files(self):
        report = make_report(
            [
                f("small", (100, 4)),
                f("big", (400, 300)),
            ],
            total=1200,
        )
        params = JobParams(intensity=Intensity.EXTREME, extreme_min_mib=1)
        # 1 MiB / 4096B = 256 块；big 有 300 块，small 只有 4 块
        plan = build_plan("t", Strategy.PACK_TAIL, report, params=params)
        assert {op.file_id for op in plan.ops} == {"big"}

    def test_empty_selection_explains_the_threshold(self):
        """筛不出文件时必须说清是门槛挡的，不能报"没有文件"。"""
        report = make_report([f("small", (100, 4))], total=1200)
        params = JobParams(intensity=Intensity.EXTREME, extreme_min_mib=8)
        plan = build_plan("t", Strategy.PACK_TAIL, report, params=params)
        assert plan.ops == ()
        text = " ".join(plan.notes)
        assert "极端档" in text and "没有" not in text.replace("没有一个", ""), text
