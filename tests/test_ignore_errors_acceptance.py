"""「忽略错误运行」的独立验收。

这份文件**不重跑**别人的用例，而是自己编排失败序列去证伪开关的承诺：

* 打开开关 = **不因连续失败停下**（两条闸门都要真的被跳过，不是恰好没撞上）；
* 打开开关 ≠ 无限容忍：累计失败到 ``MAX_IGNORED_FAILURES`` 必须停，且原因要写在
  结果里（用户只看到「任务结束了」等于没解释）；
* 开关关掉 = 默认的保守收尾**一字不变**（防回归）。

为什么用自造的 ``_RecordingExecutor`` 而不是 ``MemoryDevice.fail_lba_set``：
后者只能造"写盘失败"，而真机上最常见的是**打开文件被占用**（winerror=32），两类
失败在两条闸门上的分量完全不同。另外它顺手记录"哪些 op 真的被记账了"，
才能区分"熔断了但结果恰好对"和"熔断真的生效了"。

为什么用 300 而不是 199：199 只证明"阈值没撞上"，300 才证明"闸门被跳过"。
"""

from __future__ import annotations

import json

import pytest

from lbamover.contract.enums import FreeSpaceSide, Strategy
from lbamover.contract.models import (
    MAX_IGNORED_FAILURES,
    Extent,
    JobParams,
    MoveOp,
    Plan,
)
from lbamover.devices.memory import MemoryDevice
from lbamover.engine.checkpoint import CheckpointStore
from lbamover.engine.control import RunControl
from lbamover.engine.pipeline import OpResult
from lbamover.engine.scheduler import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_CONSECUTIVE_UNOPENABLE,
    JobRunner,
)

#: 只影响单个文件的失败阶段（真机上最常见）
STAGE_OPEN = "打开文件"
#: 系统级失败阶段（搬簇、读写、校验里的一类）
STAGE_MOVE = "搬簇"


class _RecordingExecutor:
    """按 op_id 返回预设结果，并记录**真正被执行**的 op_id。

    两个能力都是刻意的：

    * 预设结果 —— 精确编排"哪几个失败、卡在哪个阶段"，这是两条闸门的分档依据；
    * 记录 ran —— 才能断言"后面的文件到底有没有被处理"，只看计数会被
      "熔断了但刚好停在对的地方"骗过去。
    """

    def __init__(self, failures: dict[int, str], on_run=None) -> None:
        self.failures = failures
        self.on_run = on_run
        self.ran: list[int] = []

    def run_op(self, op: MoveOp, start_blocks: int = 0) -> OpResult:
        self.ran.append(op.op_id)
        if self.on_run is not None:
            self.on_run(op, self)
        stage = self.failures.get(op.op_id)
        if stage is None:
            return OpResult(op.op_id, op.blocks, op.blocks * 4096, ok=True)
        return OpResult(
            op.op_id,
            0,
            0,
            ok=False,
            error=f"{stage}失败",
            stage=stage,
            error_type="DeviceError",
            error_class="retryable",
            winerror=32 if stage == STAGE_OPEN else 1117,
        )

    def close(self) -> None:  # 与 Executor 协议保持一致
        pass


class _Run:
    """一次运行的产物。字段名直白，避免在用例里写 ``res[0][1]``。"""

    def __init__(self, result, logs: list[str], executor: _RecordingExecutor) -> None:
        self.result = result
        self.logs = logs
        self.executor = executor

    @property
    def accounted(self) -> list[int]:
        """被记账的 op_id（熔断之后新完成的 op 不会被记账）。"""
        return [r.op_id for r in self.result.op_results]

    @property
    def accounted_ok(self) -> list[int]:
        return [r.op_id for r in self.result.op_results if r.ok]


def _plan(count: int) -> Plan:
    return Plan(
        job_id="ignore-errors",
        strategy=Strategy.RELOCATE_TAIL,
        ops=tuple(
            MoveOp(
                op_id=i,
                file_id=str(i),
                path=f"/f{i}",
                src_extents=(Extent(6000 + i * 16, 8),),
                dst_start=100 + i * 32,
                verify=False,
            )
            for i in range(count)
        ),
    )


def _run(
    tmp_path,
    failures: dict[int, str],
    count: int,
    *,
    ignore_errors: bool,
    concurrency: int = 1,
    halt_on_error: bool = False,
    control: RunControl | None = None,
    on_run=None,
    store: bool = True,
) -> _Run:
    """跑一次计划。并发固定 1，让记账顺序与 op 顺序一致，断言才有确定性。

    ``store=False`` 只关掉每个 op 后强制落断点那一步（它的字节量随已完成 op 数
    增长，4000 个 op 会把用例拖到十几秒）。断点与本次验收的计数口径无关，
    而且 B 那条（1050 个 op）是开着断点跑的 —— 覆盖没丢，只是不在最慢的用例上重复。
    """
    logs: list[str] = []
    holder: dict[str, _RecordingExecutor] = {}

    def factory(_runner: JobRunner) -> _RecordingExecutor:
        executor = _RecordingExecutor(failures, on_run=on_run)
        holder["executor"] = executor
        return executor

    runner = JobRunner(
        MemoryDevice(8192, 4096),
        MemoryDevice(8192, 4096),
        JobParams(
            chunk_blocks=8,
            max_retries=0,
            concurrency=concurrency,
            ignore_errors=ignore_errors,
        ),
        control or RunControl(),
        store=CheckpointStore(tmp_path / "cp.json") if store else None,
        on_log=logs.append,
        halt_on_error=halt_on_error,
        executor_factory=factory,
    )
    return _Run(runner.run(_plan(count)), logs, holder["executor"])


class TestSwitchSkipsBothBreakers:
    """A：打开开关后，两条连续熔断都必须被真的跳过。

    两条闸门分属不同阶段（20 是系统级、200 是打不开），只测其中一条等于只测了
    一半 —— 实现里是两个 ``if``，改一处漏一处是很常见的回归形态。
    """

    @pytest.mark.parametrize(
        ("stage", "fail_prefix", "gate"),
        [
            # 300 > 200：越过「打开文件」那道闸门
            (STAGE_OPEN, MAX_CONSECUTIVE_UNOPENABLE + 100, MAX_CONSECUTIVE_UNOPENABLE),
            # 30 > 20：越过「系统性故障」那道闸门（既有用例没覆盖这条）
            (STAGE_MOVE, MAX_CONSECUTIVE_FAILURES + 10, MAX_CONSECUTIVE_FAILURES),
        ],
        ids=["打不开300个", "搬簇连续30个"],
    )
    def test_keeps_going_and_moves_the_rest(self, tmp_path, stage, fail_prefix, gate):
        assert fail_prefix > gate, "构造的失败数必须真的越过闸门，否则本条证明不了什么"
        tail_ok = 20
        failures = {i: stage for i in range(fail_prefix)}

        run = _run(
            tmp_path,
            failures,
            fail_prefix + tail_ok,
            ignore_errors=True,
        )
        res = run.result

        assert res.aborted == "", f"打开开关后不该再熔断：{res.aborted}"
        assert not res.cancelled, "跳过熔断不是取消，状态不能报成用户停止"
        assert res.failed_ops == fail_prefix, f"失败数应逐条记全：{res.failed_ops}"
        assert res.moved_ops == tail_ok, (
            f"熔断之外的 {tail_ok} 个文件必须照常搬完，实际搬了 {res.moved_ops} 个"
        )
        # 不只看计数：确认"成功的确实是尾部那 20 个"，而不是把失败也算成了成功
        assert run.accounted_ok == list(range(fail_prefix, fail_prefix + tail_ok))


class TestCumulativeCeilingStillStops:
    """B：忽略错误 ≠ 无限容忍。累计到上限必须停，并把原因讲清楚。"""

    def test_stops_exactly_at_the_ceiling(self, tmp_path):
        total = MAX_IGNORED_FAILURES + 50
        failures = {i: STAGE_OPEN for i in range(total)}

        run = _run(tmp_path, failures, total, ignore_errors=True)
        res = run.result

        assert res.aborted, "累计失败到上限必须留下原因，否则用户只看到「任务结束了」"
        assert "忽略错误运行" in res.aborted, (
            f"中止原因必须点出是用户开了这个开关，否则读起来像系统故障：{res.aborted}"
        )
        assert str(MAX_IGNORED_FAILURES) in res.aborted, (
            f"用户要知道停在哪个数，文案里得出现 {MAX_IGNORED_FAILURES}：{res.aborted}"
        )
        assert res.failed_ops == MAX_IGNORED_FAILURES, (
            f"必须正好停在 {MAX_IGNORED_FAILURES}：{res.failed_ops}"
        )
        # 上限之后完成的 op 不该被记账，否则 failed_ops 会冲过上限
        assert len(run.accounted) == MAX_IGNORED_FAILURES
        assert any("忽略错误运行" in m for m in run.logs), "中止原因也要进日志"

    def test_message_reports_how_many_ops_were_left(self, tmp_path):
        """中止文案要给"还剩多少没跑"：用户下一步就是判断要不要重跑。

        有剩余却不说，用户只能自己拿总数减失败数去猜；而这个数只有调度器知道
        （它知道队列里还剩多少个 op）。
        """
        total = MAX_IGNORED_FAILURES + 50
        failures = {i: STAGE_OPEN for i in range(total)}

        res = _run(tmp_path, failures, total, ignore_errors=True, store=False).result
        left = total - MAX_IGNORED_FAILURES

        assert f"剩余 {left} 个未执行" in res.aborted, (
            f"中止文案必须点明还有 {left} 个没跑：{res.aborted}"
        )

    def test_message_does_not_invent_leftover_work(self, tmp_path):
        """第 1000 个失败恰好是最后一个 op 时，不能再写「剩余 N 个未执行」。

        这句话不是措辞洁癖：用户会照着它去找那些根本不存在的"剩余任务"，
        或者在重跑时误以为上次还有活没干完。原来无分支的文案就是这么说的。
        """
        failures = {i: STAGE_OPEN for i in range(MAX_IGNORED_FAILURES)}

        res = _run(
            tmp_path, failures, MAX_IGNORED_FAILURES, ignore_errors=True, store=False
        ).result

        assert res.aborted, "累计到上限仍要留下原因"
        assert "未执行" not in res.aborted, f"已经跑完了，不该说还有剩余：{res.aborted}"
        assert "剩余 0" not in res.aborted, (
            f"「剩余 0 个未执行」这种说法比不说更糟，用户会去数剩下什么：{res.aborted}"
        )
        assert "已无剩余任务" in res.aborted, res.aborted


class TestDefaultBehaviourUnchanged:
    """C：开关关掉时，默认的两条闸门数值与文案都不能变。

    这是防回归：新开关最典型的破坏方式不是自己不生效，而是顺手把默认路径改坏。
    """

    def test_unopenable_still_trips_at_200(self, tmp_path):
        fail_prefix = MAX_CONSECUTIVE_UNOPENABLE + 100
        failures = {i: STAGE_OPEN for i in range(fail_prefix)}

        run = _run(tmp_path, failures, fail_prefix + 20, ignore_errors=False)
        res = run.result

        assert res.aborted, "默认（保守收尾）行为被改坏了：该熔断却没熔断"
        assert "打不开" in res.aborted, f"熔断原因必须点出是打不开：{res.aborted}"
        assert res.failed_ops == MAX_CONSECUTIVE_UNOPENABLE, (
            f"默认阈值必须仍然是 {MAX_CONSECUTIVE_UNOPENABLE}：{res.failed_ops}"
        )
        assert len(run.accounted) == MAX_CONSECUTIVE_UNOPENABLE, (
            "熔断之后完成的 op 不该继续被记账"
        )

    def test_system_stage_still_trips_at_20(self, tmp_path):
        fail_prefix = MAX_CONSECUTIVE_FAILURES + 10
        failures = {i: STAGE_MOVE for i in range(fail_prefix)}

        run = _run(tmp_path, failures, fail_prefix + 10, ignore_errors=False)
        res = run.result

        assert "系统性故障" in res.aborted, res.aborted
        assert res.failed_ops == MAX_CONSECUTIVE_FAILURES, res.failed_ops


class TestBoundaryAndInteraction:
    """D：自己挑的边界 —— 专挑"实现最容易写错、又不报错"的地方。

    选「失败与成功交替」作为主场景，理由：它是**唯一能区分"累计"与"连续"两种
    语义**的序列。实现里 ``total_failures`` 与两条 ``consecutive_*`` 计数器并存，
    若把上限误接到连续计数上，全连续失败的用例照样通过（B 就是全连续），只有
    交替序列会露馅：默认闸门在整个过程里永远够不到阈值，而"累计 1000 就停"必须
    照样生效。同一条序列跑两遍（开关开/关）做差分，比单跑一遍更能说明问题。
    """

    def test_cumulative_counts_across_interleaved_successes(self, tmp_path):
        total_failures = MAX_IGNORED_FAILURES + 50
        count = total_failures * 2  # 失败 / 成功交替
        failures = {i: STAGE_MOVE for i in range(count) if i % 2 == 0}

        on = _run(tmp_path, failures, count, ignore_errors=True, store=False).result
        assert on.aborted, "累计失败已过上限，即使连续失败数一直是 1 也必须停"
        assert "忽略错误运行" in on.aborted, on.aborted
        assert on.failed_ops == MAX_IGNORED_FAILURES, (
            f"累计口径必须停在第 {MAX_IGNORED_FAILURES} 个失败：{on.failed_ops}"
        )
        # 第 1000 个失败之前，成功与失败一一交替 → 大约搬成 999 个。
        #
        # **不能断言精确等于 999**：熔断是一次 break，此刻线程池里还有在途
        # op（记账要落断点，比执行慢），它们要么已经记上、要么还没。所以这里
        # 会浮动几个 —— 这是"报告只统计已结算的 op"这条口径的固有性质，不是
        # 少搬了数据。写成精确值会让这条用例随机变红，而红的时候看起来像
        # "累计口径算错了"，把人引向错误的方向。
        assert MAX_IGNORED_FAILURES - 20 <= on.moved_ops <= MAX_IGNORED_FAILURES, (
            on.moved_ops
        )

        # 差分：同一条序列、只把开关关掉，就**不该**停 —— 证明差异来自开关，
        # 也顺带证明默认路径判的确实是"连续"而不是"累计"。
        off = _run(tmp_path, failures, count, ignore_errors=False, store=False).result
        assert off.aborted == "", f"默认闸门判的是连续失败，交替序列不该熔断：{off.aborted}"
        assert off.failed_ops == total_failures, off.failed_ops

    def test_one_below_the_ceiling_does_not_stop(self, tmp_path):
        """999 个失败正好在上限之下：多一个就停、少一个就得跑完，差一是最容易写错的地方。"""
        fail_prefix = MAX_IGNORED_FAILURES - 1
        tail_ok = 19
        failures = {i: STAGE_OPEN for i in range(fail_prefix)}

        run = _run(tmp_path, failures, fail_prefix + tail_ok, ignore_errors=True, store=False)

        assert run.result.aborted == "", (
            f"{fail_prefix} 个失败还没到上限，不该停：{run.result.aborted}"
        )
        assert run.result.moved_ops == tail_ok, run.result.moved_ops

    def test_all_success_has_no_abort_but_says_the_switch_is_on(self, tmp_path):
        """全成功时既不该有中止原因，也该在日志里说明"开关是开着的"。"""
        run = _run(tmp_path, {}, 30, ignore_errors=True)

        assert run.result.aborted == ""
        assert run.result.moved_ops == 30
        assert any("忽略错误运行" in m for m in run.logs), (
            "一上来就要说明「后面不停是用户自己的选择」，否则日志像一个该停没停的 bug"
        )

    def test_cancelled_op_is_not_counted_as_failure(self, tmp_path):
        """用户点停止不是"失败"。若被计成失败，累计上限会被自己的取消提前打满。"""
        control = RunControl()

        def stop_after_three(_op, executor):
            if len(executor.ran) >= 3:
                control.request_cancel()

        run = _run(
            tmp_path, {}, 50, ignore_errors=True, control=control, on_run=stop_after_three
        )
        res = run.result

        assert res.cancelled, "取消必须被标记为取消"
        assert res.failed_ops == 0, f"取消不该进失败计数：{res.failed_ops}"
        assert res.aborted == "", "取消是用户主动的，不能写中止原因"
        assert len(res.failures) == 0

    def test_halt_on_error_wins_over_ignore_errors(self, tmp_path):
        """两个开关同时打开时，fail-fast 优先 —— 把优先级钉住，别让它随改动漂移。

        顺序上 ``halt_on_error`` 在 ``ignore_errors`` 之前判断：勾了"遇到错误
        立刻停"却又希望"忽略错误跑完"是自相矛盾的组合，此时选更保守的那个，
        并且结果必须是 ``cancelled`` 而不是"中止"（它是主动请求的停止）。
        """
        failures = {i: STAGE_OPEN for i in range(10)}

        run = _run(
            tmp_path,
            failures,
            30,
            ignore_errors=True,
            halt_on_error=True,
        )
        res = run.result

        assert res.cancelled, "fail-fast 是一次主动取消"
        assert res.failed_ops == 1, f"fail-fast 应在第 1 个失败就停：{res.failed_ops}"
        assert res.aborted == "", "fail-fast 走的是取消路径，不该同时挂中止原因"


class TestSwitchReachesTheRunner:
    """开关必须真的落在 ``JobParams`` 上，而不是"解析了被丢掉"。"""

    def test_params_carry_the_flag_by_default_off(self):
        assert JobParams().ignore_errors is False, "默认必须是保守收尾"

    def test_with_overrides_keeps_the_flag(self):
        on = JobParams(ignore_errors=True)
        assert on.with_overrides(chunk_blocks=16).ignore_errors is True

    def test_flag_survives_dict_roundtrip(self):
        """开关必须过得了序列化往返：参数一旦被落盘/回放，勾过的东西不能变回去。

        这条原来挂着 ``xfail``：``to_dict`` 当时手写键名、漏了 ``ignore_errors``，
        往返后静默丢成 False。源码改成按 ``fields(self)`` 自动导出后转绿，
        标记随之摘掉 —— 它现在是一条正常的回归用例。

        **构造必须用非默认值**：默认值相等会骗过断言（``test_models`` 里那条
        ``from_dict(p.to_dict()) == p`` 之所以长期是绿的，就是因为漏掉的字段恰好
        都等于默认值）。另外补一次 JSON 往返：真正落盘走的是 JSON，枚举字段在
        那一步才会暴露"能进不能出"。
        """
        on = JobParams(ignore_errors=True)

        assert JobParams.from_dict(on.to_dict()).ignore_errors is True
        # 落盘回放的真实路径是 JSON，不是内存里的 dict
        assert JobParams.from_dict(json.loads(json.dumps(on.to_dict()))).ignore_errors is True
        # 反方向也要成立：没有残留的"总是 True"
        assert JobParams.from_dict(JobParams().to_dict()).ignore_errors is False


class TestRealEntryPoint:
    """真实入口：``python -m lbamover selftest`` 这条路径也要把开关送到跑批那一步。

    为什么不能只验 ``build_parser().parse_args()``：那只证明"解析出来了"，
    而这类缺陷恰恰出在"解析出来之后被忘在中间某一层"——参数一路经过
    ``cli → JobParams → EngineCore.params → JobRunner``，任何一层复制时漏字段
    都会表现为"勾了没反应"。所以这里在**真正的 ``JobRunner.run`` 上装探针**，
    直接看它拿到的 ``params.ignore_errors``。

    用 ``--blocks 512 --files 8`` 的小合成卷：全部成功，因此默认路径与开关路径
    的**输出完全一样**（这点本身也是结论：开关不该改变全成功场景的行为），
    只能靠探针分辨。
    """

    def _run_cli(self, monkeypatch, capsys, *extra: str) -> tuple[int, bool | None]:
        from lbamover import cli
        from lbamover.engine import engine as engine_module

        seen: dict[str, bool] = {}

        class _SpyRunner(engine_module.JobRunner):
            def run(self, plan, resume=None):
                seen["ignore_errors"] = self.params.ignore_errors
                return super().run(plan, resume=resume)

        monkeypatch.setattr(engine_module, "JobRunner", _SpyRunner)
        code = cli.main(["selftest", "--blocks", "512", "--files", "8", *extra])
        capsys.readouterr()  # 吞掉 selftest 的正常输出
        return code, seen.get("ignore_errors")

    def test_flag_arrives_at_the_runner(self, monkeypatch, capsys):
        code, flag = self._run_cli(monkeypatch, capsys, "--ignore-errors")
        assert code == 0, "selftest 本身必须 PASS"
        assert flag is True, "开关被解析出来却没能到 JobRunner —— 等于没做"

    def test_flag_absent_by_default(self, monkeypatch, capsys):
        code, flag = self._run_cli(monkeypatch, capsys)
        assert code == 0
        assert flag is False, "不带参数时必须走保守收尾"


@pytest.fixture
def qapp():
    """按需起一个 QApplication；没装 PySide6 就跳过，不拖累上面那些纯引擎用例。"""
    pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面用例")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class TestParamsPanelSwitch:
    """界面那一侧：默认不勾、勾了要进 ``JobParams``、运行中要锁住。

    这几条以前一条测试都没有（全仓库 ``ignore_errors`` 只出现在 src 里）。界面
    是最容易"看起来做了其实没接线"的一层：复选框存在、``current()`` 里漏了
    一行，用户勾了照样无效，而日志与引擎那边完全正常。
    """

    def test_default_off_and_toggling_reaches_params(self, qapp):
        from lbamover.ui.widgets.params_panel import ParamsPanel

        panel = ParamsPanel()
        assert panel.current().ignore_errors is False, "默认必须是保守收尾（不勾）"
        panel.ignore_errors.setChecked(True)
        assert panel.current().ignore_errors is True, "勾上了却没进 JobParams"

    def test_tooltip_promises_the_contract_number(self, qapp):
        """界面对用户承诺的数字必须与引擎用的是同一个。

        契约层专门把 ``MAX_IGNORED_FAILURES`` 放在 models 里就是为了这件事：
        写两份就是等着它们漂移。widget 不许 import engine，所以它只能来自契约。
        """
        from lbamover.ui.widgets.params_panel import ParamsPanel

        panel = ParamsPanel()
        assert str(MAX_IGNORED_FAILURES) in panel.ignore_errors.toolTip()

    def test_locked_while_running(self, qapp):
        """闸门在 ``JobRunner`` 构造时就定了，运行中改只会让界面与实际行为说法不一。"""
        from lbamover.ui.widgets.params_panel import ParamsPanel

        panel = ParamsPanel()
        panel.set_locked(True)
        assert not panel.ignore_errors.isEnabled(), "运行期间必须锁住「忽略错误运行」"
        panel.set_locked(False)
        assert panel.ignore_errors.isEnabled()


class TestParamsKeySetContract:
    """序列化的**结构**契约：``to_dict`` 的键必须与 dataclass 字段一模一样。

    为什么还要单独测结构：往返相等的用例（包括 ``test_models`` 里那条老用例）用
    默认值构造时是**测不出漏字段**的 —— 漏掉的字段正好等于默认值，``==`` 恒真，
    于是它绿了很多轮，而真实的 ``ignore_errors`` 开关早就在回放时被丢掉了。
    比键集合才与具体值无关：以后再加字段，无论是忘了导出（少键）还是留了已删字段
    （多键），都会立刻红，并且失败消息直接点名是哪个字段。

    这条与 Engineer 在 ``test_models.py`` 里那条功能重叠是**有意的**：修缺陷的人
    自己写的用例属于自证，验收侧要有一份独立的、从契约出发的检查。
    """

    def test_to_dict_keys_equal_declared_fields(self):
        from dataclasses import fields

        dumped = JobParams(ignore_errors=True).to_dict()
        declared = {f.name for f in fields(JobParams)}
        missing = sorted(declared - set(dumped))
        extra = sorted(set(dumped) - declared)

        assert not missing and not extra, (
            "JobParams.to_dict() 的键与 dataclass 字段不一致。"
            f"漏导出：{missing}（这些字段落盘回放后会静默变回默认值）；"
            f"多出未知键：{extra}（from_dict 会忽略它们，等于白写）"
        )

    def test_every_field_survives_a_json_roundtrip(self):
        """逐字段比，而不是整体 ``==``：整体只回一个 False，指不出是哪一项。"""
        on = JobParams(
            block_size=512,
            chunk_blocks=7,
            queue_depth=3,
            concurrency=5,
            max_retries=9,
            retry_base_ms=11,
            retry_max_ms=999,
            rate_limit_mbps=12.5,
            verify=False,
            checkpoint_every_chunks=1,
            allow_raw_write_on_mounted=True,
            reserve_tail_blocks=13,
            reclaim_blocks=17,
            reclaim_at=FreeSpaceSide.HEAD,
            reclaim_avoid_protected=False,
            ignore_errors=True,
        )
        # 落盘走的是 JSON 文本，不是内存里的 dict：枚举与元组都在这一步变样
        back = JobParams.from_dict(json.loads(json.dumps(on.to_dict())))

        from dataclasses import fields

        broken = [
            f.name
            for f in fields(JobParams)
            if getattr(back, f.name) != getattr(on, f.name)
        ]
        assert broken == [], f"这些字段过不了 JSON 往返：{broken}"
        assert back == on

    def test_guard_itself_catches_a_dropped_field(self, monkeypatch):
        """守卫不能是空转：把 ``ignore_errors`` 从导出里摘掉，它必须红并点名。

        不这么试一次，上面那条"键集合相等"就只是又一条恒真的测试 —— 而恒真的
        守卫比没有守卫更糟：它让人以为这件事有人看着。
        """
        original = JobParams.to_dict

        def dropped(self):
            data = original(self)
            data.pop("ignore_errors")
            return data

        monkeypatch.setattr(JobParams, "to_dict", dropped)
        with pytest.raises(AssertionError) as excinfo:
            self.test_to_dict_keys_equal_declared_fields()

        message = str(excinfo.value)
        assert "漏导出" in message, message
        assert "ignore_errors" in message, message
