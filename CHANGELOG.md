# 更新日志（CHANGELOG）

本文件记录 LBA Tail Mover 的版本变更，**一律使用中文**（提交信息 commit 同样使用中文）。

## 记录规范

按版本层级分三级记录：

1. **字母尾缀版本**（如 `1.0.0A` / `1.0.0B`）：每个字母修订**各自保留独立的更新记录**；
2. **小版本**（如 `1.0.0`）：单独列一条「小版本更新」条目，汇总该小版本（含其下各字母修订）；
3. **大版本跨越**（如 `V1 → V2`）：整合为「V1 → V2 更新事项」，并**另列一节「新增事项」**专述新功能。

---

## V2 · 引擎重构版

> V2 是相对 V1 的整体重构：从「只能把大文件堆到卷尾」升级为「可指定任意 LBA 区间、
> 带安全保留与挡路者自动整理、MFT 全卷扫描」的迁移引擎。应用版本号自 V2 起记为 `1.0.0`。

### V1 → V2 更新事项（整合）

- 迁移目标从「仅卷尾预留区」扩展为「任意可拖选 LBA 区间」（万向移动）；
- 规划前增加可行性三档预判（直接可行 / 需驱逐 / 不可行 + 量化原因）；
- 扫描从「逐文件句柄 IOCTL」升级为「直读 `$MFT` + 句柄」双通道，全卷可见、可标注可搬性；
- 执行前重读位图构造影子位图，驱逐 / 中转计划不再因过期快照被整批拒绝；
- 调度引入依赖图与电梯序，文件间依赖环用中转（relay）打断；
- 引擎层改为纯函数 + dataclass，补齐单元测试（现 258 例）。

### 新增事项（V2 新功能）

- **N1 磁盘安全保留范围**：卷首 / 卷尾 / MFT 三道几何禁区 + 对象禁区（`$` 元数据 / 压缩 / EFS / 无物理分配）+ 规划断言与执行前 `SafetyGuard` 双重校验；分区表在卷外、物理上不可达。
- **N2 LBA 万向移动**：在布局图上拖选目标区间，把选中文件连续装入，区间内挡路者自动迁出。
- **N3 内存加速（可调缓冲）**：HDD 寻道惩罚探测（`StorageDeviceSeekPenaltyProperty`）+ 批次合并 + 预读预热 + 电梯序 + 可调缓冲 / 批大小。
- **N4 挡路碎片自动整理**：识别区间内挡路文件并迁出；被独占 / 元数据等不可搬者计入「归属未知」使可行性诚实降级；依赖环 relay 断裂。
- **N5 MFT 扫描与安全控制**：`USA fixup` → 属性链（含 `$ATTRIBUTE_LIST` 回退）→ runlist 解码；双通道 + 自动回退（原因码）+ 抽样复核 + 逐记录边界校验，严格只读。

### 小版本 1.0.0

- V2 引擎 + PySide6 完整界面：无边框、自绘 LBA 布局图、双主题、拖选目标区间、可行性徽章、挡路清单、IO 加速面板、迁移摘要、执行与回滚。
- 双模式：默认「预览」**绝不写盘**；「执行」经危险确认后走唯一写路径 `FSCTL_MOVE_FILE`。

### 字母尾缀更新记录

#### 1.0.0E

- **修复：扫描 / 归属索引被「取消」时误报成功（P2，QA 实测）**：`MftScanOutcome.ok` 的语义是「数据可用」而非「没被取消」，取消分支此前只 `notes.append("扫描已取消（结果不完整）")` 就 `break`，**不设 `fallback_code`** → `ok` 仍为 `True` → `MftScanWorker.run` 走**成功**分支（`completed` 发一次、`fallback`/`failed` 都不发），而 `outcome.notes` 从不进入任何信号 → 用户按「取消」后看到的是「扫描完成」+ 一份**不完整**文件列表、零提示（可能误以为某文件不在本卷）。修复：①`MftScanOutcome` 新增 `cancelled: bool` 字段，取消分支置位（**`ok` 语义保持「数据可用」，不把取消当失败**）；②`MftScanWorker` 同步自身取消标记并在 `completed` 前把阶段文案改为「扫描已取消，仅显示已解析的 N 项」；③`ScanWorker`（句柄通道两处 `completed.emit(部分结果)`）/ `IndexWorker`（取消仍会 `ready`）各新增只读 `cancelled` 属性暴露取消态；④UI 对应分支：`_on_scan_completed` 检出取消 → 只报「扫描已取消…」、**不自动勾选**、**不建归属索引**（不拿部分数据建全卷索引）、**自行退出忙碌态**（正常路径靠索引回调释放，取消路径不建索引）；`_on_index_ready` 检出取消 → 只报「归属索引已取消（未完成）」并把挡路者归属标记为不可用、**不把部分索引当归属依据**。回归钉 `tests/test_cancel_semantics.py`（8 例：`ok` 语义、worker 取消只发 `completed` 无 `fallback`/`failed`、进度文案带「已取消」、两个 worker 的 `cancelled`、UI 取消路径 vs 正常路径对拍）。
- **修复：`_on_plan_worker_finished` 缺少 `sender() is None` 防御（P3，QA 复核）**：该槽原本在直调（非信号驱动）时也会「接管收尾」，会清掉一个**可能仍在运行**的规划槽位（`_plan_worker = None` + `_set_busy(False)` 而线程还在跑）→ 界面误判空闲、用户可再点一次生成计划，旧线程 `completed` 回来时槽位已是新值（双重规划线程）。现补 `if worker is None: return`，并把契约写进 docstring（**只由 `QThread.finished` 驱动，直调为 no-op**）。
- **文档订正**：1.0.0D 条目与 `app/core/scanner.py::PlanWorker.request_cancel` 中「只有 `PlanWorker` 是裸 return」改为「**唯一需要新增 `finished` 兜底的是 `PlanWorker`**（`FeasibilityWorker` 同为裸 return，但早已接 `finished` 且不参与 `_busy`）」。
- **`sender()` 读取点一次性清点（第 4 次踩同一个坑后的根治）**：`sender()` 只在信号派发期间有值，直调 / 换信号源时是 `None`。逐点判定后处理如下：

  | 槽 | `sender()` 用途 | 是否加 `_xxx_worker` 兜底 | 理由 |
  |---|---|---|---|
  | `_on_scan_completed` | 读 `cancelled` | **是**（`_scan_worker`） | 直调时丢取消会被当成「完成」；该 worker 此刻尚未置 None，可读 |
  | `_on_index_ready` | 读 `cancelled` | **是**（`_index_worker`，且必须在置 None 之前读） | 同上；否则把取消索引写成归属表 |
  | `_on_plan_worker_finished` | 身份比对（判断是不是当前 worker） | **否**（反而加 `is None → return`） | 兜底会误清「可能仍在跑」的规划槽位，见 1.0.0E 前一条 |
  | `_take_resume_intent`（`_on_plan_ready` / `_on_plan_failed` 共用） | 读一次性接续意图 `resume_after` | **否**（只认 sender + 窗口瞬时标志） | 意图误判的后果是「无端弹执行确认框」（危险方向），漏判只是「少一次自动接续」（安全方向）；且 `_plan_worker` 可能已是新线程，读它等于把上一轮意图错配给这一轮 |
  | `_on_feasibility_ready` / `_on_feasibility_failed` | 不使用 `sender()`（用 `seq` 代次） | 不适用 | 代次比对已天然区分新旧结果 |

  两枚直调钉子专防复发：`test_ui_scan_completed_direct_call_uses_worker_fallback` / `test_ui_index_ready_direct_call_uses_worker_fallback`。
- **调用点钉子（QA 指出的「钉函数 ≠ 钉调用点」）**：`mark_cancelled()` 抽成函数后，`test_mft_reader_marks_cancel_on_outcome` 只钉住了函数本身 —— 若把 `scan_volume_mft` 里那行调用删掉（`break` 留着），函数钉子仍全绿而取消语义又丢了。新增 AST 钉子 `test_scan_volume_mft_cancel_branch_calls_mark_cancelled`：扫 `scan_volume_mft` 源码，断言「守卫 `should_cancel` 的 `if` 分支体内同时存在 `mark_cancelled(...)` 调用与 `break`」且恰好一处。变异验证：删掉调用点 → 仅该钉子变红（函数钉子仍绿），证明它钉的正是「接线」而非「实现」。
- **回归钉方法论**：涉及 `self.sender()` 的槽一律用**真 Qt 信号**驱动（纯 Python 替身的 `emit()` 不设 sender，直调更是 `sender() is None`）。据此把 `test_plan_cancel_fallback_clears_busy` / `test_plan_finished_is_noop_after_terminal_signal` 从「直调槽」改为 `_SignalSource.finished.emit()`，并新增 `test_plan_finished_direct_call_is_noop` 钉住「直调 = no-op」契约（`tests/test_ui_execute_flow.py` 共 14 例）。
- **回归钉反向验证（跑完即还原，未留在仓库）**：①兜底槽还原为修复前写法（`if worker is not None and worker is not self._plan_worker`）→ `test_plan_finished_direct_call_is_noop` 变红（**证实 P3-1 的洞是真的**：`sender() is None` 时确实会接管收尾）；②去掉 `_on_scan_completed` 的取消分支 → `test_ui_scan_completed_reports_cancelled` 红；③`mark_cancelled` 只留 note 不置 `cancelled` → `test_mft_reader_marks_cancel_on_outcome` + `test_mft_scan_worker_cancel_reports_completed_with_flag` 红（说明为何要把这一行抽成无设备依赖的纯函数：`MftScanWorker` 的钉子桩掉了 `scan_volume_mft`，覆盖不到它）；④去掉 `_on_index_ready` 的取消分支 → `test_ui_index_ready_reports_cancelled` 红；⑤去掉 `_on_scan_completed` 的 `_scan_worker` 兜底 → `test_ui_scan_completed_direct_call_uses_worker_fallback` 红；⑥去掉 `_on_index_ready` 的 `_index_worker` 兜底 → 对应直调钉子红；⑦把兜底的读取顺序改成「先置 None 再读」→ 同样红（顺序也是契约）；原样代码 14 + 11 例全绿。
- **测试稳定性（同批，非功能改动）**：`tests/test_ownership.py::test_query_cost_does_not_scale_linearly` 在整机负载下偶发假红（本轮全量跑出现 1 次，单独重跑 3 次全绿）。加固为「两侧各 5 轮取最优、比值取两侧最优之比（原先是取 ratio 的最优，small 被测快时虚高）、打印实测值、阈值 3.0 → 4.0」——实测稳定区间 1.74~1.81（10× 数据量），线性退化会接近 10，4.0 距实测约 2.2× 余量、距线性约 2.5×，仍能拦住复杂度回归。
- 版本号 `1.0.0D → 1.0.0E`。

#### 1.0.0D

- **界面性能优化**：
  - 挡路者定位算法热点修复：`packer._clip_extent` 由「每个 extent 线性遍历全部占用区间」改为 `bisect` 二分定位 + 命中段内推进（O(E×R) → O(E log R + 命中数)），10 万 extent × 5 万占用区间规模下 `find_blockers` 从秒级降到毫秒级（新增性能回归测试并打印实测值）；
  - LBA 布局图渲染缓存：条带图像（迁移前 / 后两张 QImage）按「数据代次 + 宽度 + 主题」缓存，悬停不再整图重绘（只失效气泡新旧位置的并集矩形）；
  - 拖选目标区间改为「松手时一次性发信号」：拖动中只画紫色预览框（视觉反馈不变），不再每个 mouse move 触发主窗口连锁重算；
  - 主窗口事件路径去重：`checked_entries()` 单次计算向下传递；`_sync_buttons` 记忆上次状态，全等时跳过 `setText` / `set_role`（避免 QSS 反复 unpolish+polish）；文件表悬停只重绘旧行 + 新行两矩形、delegate 字体按字号缓存；扫描结果批量插入（64 条或 100ms 定时器）。
- **配置「确认后提交」流程**：目标区间 / IO 面板改为「暂存—应用」语义——控件修改先进入待定态（`pendingChanged` 徽章 + 「应用配置（N 项变更）」按钮 + `Ctrl+Enter` 应用 / 「放弃」还原），**当前计划始终基于已提交参数**；布局图拖选与「排除挡路者」两条路径保持即时生效（架构师明确要求的直通路径）；执行模式下存在未应用配置时点「开始迁移」先弹三选确认（先应用再执行 / 按现有计划执行 / 取消），防止「计划与参数不一致」被静默带入写盘流程。
- **可行性计算移出 UI 线程**：新增 `FeasibilityWorker`（QThread，只读），UI 线程只做参数打包与结果回显；结果返回时参数已变则丢弃并按最新参数重算，防抖 140ms 保留。大卷上拖动区间滑块不再卡顿界面。
- **执行日志批量刷盘**（方案 A，降低执行期日志 I/O 等待）：`MoveJournal.append()` 改为「每行 write + flush、fsync 攒批」——攒满 32 行（`JOURNAL_FSYNC_EVERY`，模块级常量可覆盖）或距上次 fsync 超 2 秒（`JOURNAL_FSYNC_SECONDS`）才真正 `os.fsync`；断电窗口从「1 行」如实放宽为「最多 32 行 / 2 秒」，**数据本身不受影响**（FSCTL 已完成），受影响的是回滚清单完整性。新增 `force_fsync()`，并在三个硬要求时点强制刷盘：执行 worker 结束（含取消 / 异常）、回滚 worker 启动前、回滚结束；`close()` 兜底。fsync 计数测试（假 os 替身）：写 1 行 0 次 / 32 行恰 1 次 / 超时后第 33 行第 2 次 / `force_fsync` 立即。
- **流式 disk 缓存去写放大**（方案 D）：①暂存**写合并**——`staged_copy` 的 `load` 不再逐 block 写暂存盘，攒满 `buffer_blocks` 个 block（或流末尾）后把连续块区间拼成单次顺序大写（功能输出逐字节不变；附带修掉「暂存文件在句柄未关时删除失败而残留」的旧问题）；②disk 模式 `cache_dir=None` **默认暂存在源文件所在卷根** `.lba-tmp-scratch\`（首文件决定卷，用完清理，卷根不可写回落 `%TEMP%`），主窗口不再拦截「硬盘缓存需设定暂存目录」；③UI 照实提示「暂存盘会多一倍写入，内存充足时建议内存缓存」。对拍：disk 与 memory 逐字节一致；5 块 buffer=2 暂存写从 5 次 → 3 次。
- **修复（QA 验证 1.0.0D 系列提交后实锤项）**：
  - **B-1（P1）执行三选确认两个动作按钮全部等价于「取消」**：`ChoiceDialog` 曾把按钮文字存进 `choice`，调用方按代号比对的「先应用再执行 / 按现有计划执行」两分支永不可达；改为 `choices=(代号, 文字, 角色)` 三元组、`choice` 存代号，执行确认框恢复逃生门（回归钉 `tests/test_dialogs.py`：编程点击断言 choice == "apply" / "proceed"）。
  - **B-3（P2）closeEvent 取消清单缺 `_stream_worker`**（1.0.0C 遗留）：已补齐；回归钉 `tests/test_worker_lifecycle.py` 用 AST 扫描 MainWindow 源码中所有 `self._x_worker` 属性，断言 closeEvent 逐个覆盖、运行时取值为 None 或 QThread。
  - **B-4（P2）TargetPanel pending 的 guard 不对称**：`_emit_pending` 补 guard 检查且 dirty 由快照实算（clean 时的偶发调用只会发出 `(False, 0)`，不再闪假 True）；`discard_pending` 的视觉同步段包进 guard。
  - **B-5（P3）性能基准口径**：`test_blockers_perf.py` 标注场景命中率（50% 挡路者 + 50% 陈旧索引）与 QA 的 100% 命中场景实测（431ms）不可直接对比；预算按 QA 数字放宽为 600ms。
  - **B-6（P3）可行性记忆键 `id(snapshot)` 地址复用风险**：memo 同时持有 snapshot **强引用**（`_feasibility_memo_snapshot`），memo 存活期间地址不可能被复用；签名本身去掉 `id` 项。
  - **B-7（P3）文档/预览小修**：`pending_count` 文档改为「2 组」实际口径；quick-tail 预览的卷尾守卫改用 `_live_reserve_mb()`（待定的保留区修改立刻在预览可见，与面板 `_live_*` 回显一致）。

  - **C-1（P2，QA 对方案 D 的专项审计）进程被杀时 stage 文件残留且无人清理**：`staged_copy` 的 `os.remove(scratch_path)` 挪进 finally（句柄关闭后删，异常路径同样清现场，删除失败不掩盖原异常）；`BlockStreamWorker` 收尾改为「先删目录内全部 `.stage` 残留（跨文件积累的也一并清）→ 清空后 rmdir，非 stage 文件不动、目录保留」；开发期实证残留 `C:\.lba-tmp-scratch\o.bin.stage` 已清理并 rmdir 空目录。回归钉：注入 dst 写入中途异常断言 stage 被清、预置 `.stage` 残留 + 外来文件的 worker 收尾清场对拍。
  - **执行前「先应用再执行」改为应用后自动重新规划并接续执行**：修复前该分支只提示"请重新生成迁移计划"就 return，按钮文字承诺"再执行"而实际一簇未搬，用户实机感知为「点了执行却不执行」；现改为「应用配置 → 自动 `_generate_plan()` → 新计划就绪后自动接续执行流程」（新增 `_resume_execute_after_plan` 标志，接续仍会再走 `DangerConfirmDialog`，**不跳过人工确认**；接续一次后复位，规划失败时清标志防悬挂）。同时补两处静默返回的提示：卷/位图尚未就绪、尚未生成迁移计划。回归钉 `tests/test_ui_execute_flow.py`（6 例）。
  - **QA 对上一提交（800f4a4）复核的两条同批修复**：
    - **「自动接续」意图悬挂（P2）**：`_resume_execute_after_plan` 原本是窗口全局状态，`_generate_plan` 里有 4 处 early-return（卷/位图未就绪、未勾选文件、区间非法、区间不可行）以及 `_on_plan_ready` 收到非 `MovePlan` 那条 return 都不会清它 —— 用户下次只是正常点「生成计划」时，`_on_plan_ready` 末尾的接续块会被误触发，**意外弹一次危险确认框**。改为把意图绑到**本次规划**：`_generate_plan` 入口立刻摘下窗口标志并挂到 `worker.resume_after`，`_on_plan_ready` / `_on_plan_failed` / `finished` 兜底统一经 `_take_resume_intent()` 一次性消费（消费即清）；早退时提示「配置已应用，自动执行已取消，请调整后手动点击开始迁移」。
    - **规划中按 Esc → UI 永久卡死（P1，既存但被自动重新规划放大）**：`PlanWorker.run` 在已请求取消时直接 `return`，`completed` / `failed` 一个都不发，而 UI 只连了这两个信号 —— 于是 `_set_busy(False)` 永不执行、`_plan_worker` 永不置空，进度条一直转、按钮全禁、只能关窗。现将 `worker.finished` 接到新增的 `_on_plan_worker_finished()` 兜底：仅在「线程已结束但没有任何终态信号被处理过」（`_plan_worker` 仍非空）时接管收尾（清槽位 + 退忙碌态 + 作废意图 + 提示「规划已取消」），正常收尾后为 no-op，不误清别的任务的忙碌态。已核对同一模式的其余 worker：`ScanWorker` / `MftScanWorker` / `IndexWorker` / `RollbackWorker` / `BlockStreamWorker` 取消路径都会发终态信号；**唯一需要新增 `finished` 兜底的是 `PlanWorker`**（`FeasibilityWorker` 同为「取消即裸 return」，但它早已接 `finished` 且不参与 `_busy`，因此不缺这层兜底）。
    - 回归钉 6 例 → **13 例**（新增：apply 后三条 early-return 路径均不留悬挂意图、意图绑到 worker 且 `finished` 必连、`_take_resume_intent` 一次性消费、取消兜底退出忙碌态、正常收尾后 `finished` 为 no-op、**无意图时危险确认框实例化 0 次**、**带意图时恰好 1 次且重复 `completed` 不再弹**）；另用离屏脚本对真实 `PlanWorker` 复刻「start → request_cancel → wait」验证取消后 `busy=False` / `_plan_worker=None` / 意图作废。反向验证（跑完即还原，未留在仓库）：apply 分支改回「只 notice 就 return」→ `test_apply_branch_triggers_replan_and_flag` 变红；意图改回窗口级标志（入口不清、消费不清）→ `test_early_return_after_apply_does_not_leave_intent` / `test_intent_is_bound_to_plan_worker` / `test_plan_ready_resumes_execution_once` / `test_plan_failure_clears_flag` / `test_take_resume_intent_consumes_worker_and_window` 共 5 例变红；去掉 `finished` 兜底接线 → `test_intent_is_bound_to_plan_worker` 变红；原样代码 13 例全绿。
- 版本号 `1.0.0C → 1.0.0D`。

---

#### 1.0.0A · 首次打包发布

- 新增 PyInstaller 单文件 EXE 打包（`LBA-Tail-Mover.spec` + `tools/version_info.txt`），无控制台、双击即用；
- `app.win32.mover` 等「函数内动态 import」模块在打包配置里以 hidden-import 显式声明；
- 新增 `--selftest` 自检（验证唯一写路径等关键模块确已随包）；
- 引入「三段号 + 字母」版本约定（`__version__` 三段 + `__revision__` 字母）；
- README 增补 N1–N5 功能说明与「打包为单文件 EXE」章节。

#### 1.0.0B

- **修复「执行时提示卷序列号变化」误报**：卷身份校验曾跨 API 比较（规划期 `GetVolumeInformation` 32 位 vs 执行期 `FSCTL_GET_NTFS_VOLUME_DATA` 64 位），同一卷被误拒；改为两边同源（`volume.read_volume_serial`）。
- EXE **默认以管理员启动**（`uac_admin=True`，嵌入 `requireAdministrator` 清单；可用 `LBA_NO_ADMIN=1` 出免提权调试版）。
- README 加强安全警示：顶部高危横幅 + 完整「三、风险声明与安全须知」。
- 接入 **testflow 测试工具流**（通用测试流水线，`python tools/testflow.py` 唯一入口；新增 `tools/run_all_tests.py` 适配器，把各测试文件聚合为 pytest 风格输出喂内置解析器）。
- 版本号 `1.0.0A → 1.0.0B`。

#### 1.0.0C

- **策略① 全池自动填满末端**（`densify.auto_fill_tail`）：无需手动勾选，从所有可移动文件里自动挑（大文件优先、连续落位、放不下就跳过）填入末端预留区的空闲空间，尽量填满、最大化腾出前端连续空间；`note` 报告填充率。
- **策略② 选定区间预密实化**（`densify.compact_range`）：把选定区间内的可移动文件密集重排（可打包空间 = 区间空闲 ∪ 这些文件自身将腾出的簇，并避开区间内不可搬障碍），把碎片空闲 coalesce 成更大的连续块。
- **策略③ 按文件大小分区段**（`densify.plan_size_zones`）：把区间按尺寸类切成连续区段（需求优先、从大到小），同尺寸的文件聚在同一段里密实排布；区间超发时优先保住大文件。
- 三者为**纯规划函数**（新模块 `app/core/densify.py`），只算目标布局，搬运顺序 / 中转交由既有 `scheduler`；新增 `tests/test_densify.py`（11 例）。
- **用户态分块流式搬迁模式**（新模块 `app/core/block_stream.py` + `app/win32/file_io.py`）：以 **65536 簇为 block**，可调缓存按 block 的**倍数**积攒；双阶段流水线（`load`：源→暂存 / `store`：暂存→目的）重叠推进，一个分块走了立马顶上，缓解 HDD 半双工，数据经文件系统落地、源簇读出即被重占用。两种**缓存模式**经 `file_io.copy_stream(mode=..., cache_dir=...)` 统一入口：`memory`（内存分配缓存，块在 RAM 中转）与 `disk`（设定缓存目录的硬盘缓存，块落暂存盘再读出）。新增 `tests/test_block_stream.py`（12 例）+ `tests/test_file_io.py`（4 例，对临时文件真实读写）。
- **卷前半大文件扫描**（`densify.front_half_report`）：把扫描到的大文件按前半卷 `[0, total//2)` 归类，挑出占前部、应搬末端以腾出连续空间的候选（跨中线按前半占比计），`min_clusters` 过滤小文件，统计 `front_occupied` / `movable_front`。`tests/test_densify.py` 增 4 例。
- **UI 接入**：目标区间面板新增「规划策略」选择（区间装箱 / 自动填满末端 / 区间预密实化 / 按大小分区），经 `PlanWorker` 新 mode 复用既有计划 / 执行流程；扫描完成自动做「卷前半大文件」标注（「前半·候选」状态色）。分块流式为**同卷内「前端→卷尾」**搬迁（**无外部目的地**）：`BlockStreamWorker` 逐文件「预填充一段连续簇 → 流式读出+内存缓存 → 流式覆盖 → 校验 → 源移回收站（可恢复）→ 改回原路径」。**分块流式后并入 LBA 模式：不再设独立工作模式与侧栏切换，降级为「IO 加速」面板内的执行引擎二选一（LBA 簇级迁移 / 分块流式迁移）**——规划 / 目标 / 策略流程不变，仅执行时按所选引擎分发；流式引擎免规划，主按钮变为「开始分块流式迁移」，含缓存模式（内存 / 硬盘 + 暂存目录）与缓冲倍数设置；**流式处理默认只填充末端**（引擎提示与说明中明示）。
- README 增加「代码由 AI 助手生成、维护者不具备审查能力」的来源声明。
- 本版**仅改源码 + 离线测试**，未重新打包（`dist/` EXE 仍为 1.0.0B，待收工再打包）。
- 文档拆分：原理与深入说明（架构 / Win32 契约 / 介质探测 / 安全模型 / 规划算法 / v2 实测 / N1–N5 / 1.0.0C 策略 / 打包测试要点）移至独立的 `manual_Datasheet.md`，README 精简为上手指南。
- 版本号 `1.0.0B → 1.0.0C`。

---

## V1 · 初版

- 单功能：把卷上的大文件移动到卷**末端 LBA** 区间，在前部腾出连续可用空间（为分区缩容等做准备）。
- PySide6 无边框界面：自绘 LBA 布局图（迁移前 / 后对比）、大文件列表（碎片徽章 / 大小条 / 排序）、浅色 / 深色主题。
- 预览 / 执行双模式；唯一写路径 `FSCTL_MOVE_FILE`，位图逐簇校验目标空闲；自定义危险确认对话框。
- 全部 Win32 经 `ctypes`，唯一第三方依赖 PySide6。
