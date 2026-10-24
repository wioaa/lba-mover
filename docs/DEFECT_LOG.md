# 缺陷日志（DEFECT LOG）

> 本文件由「沉淀期与缺陷修复期」的代码审查生成，供**未来更高级别的 AI 模型**接手修复。
> 状态字段：
> - **待修复**：已确认/疑似存在缺陷，尚未修复；
> - **需深入验证**：定位到可疑区域，但未在真机/单测层面坐实，需进一步确认；
> - **已修复**：本轮已解决，留痕供追溯；
> - **非缺陷（已坐实）**：曾被怀疑，经逐分支穷举 / 单测证明现有实现正确，代码不必改动。
>
> 严重程度：高（数据损坏 / 安全绕过）> 中（功能失败 / 资源泄漏 / 可触发的错误路径）> 低（统计口径 / 性能 / 边界）。

---

## 已修复（本轮审查）

### D-001 · [Glue] 执行引擎切换 / 计划过期导致主按钮无效 — 已修复
- **位置**：`app/ui/main_window.py`（`_sync_buttons` / `_on_primary_action`）、`app/ui/io_panel.py`（`_on_engine_changed` / `_emit_pending`）
- **根因**：① 流式引擎切换被并进「应用配置」暂存，切完必须再点「应用」才提交，按钮停在 LBA 的灰态；② LBA 计划过期时「生成迁移计划」按钮被 `feasibility.ok` 判定卡灰（`feasibility` 为 `None` 时直接灰掉）。
- **修复**：引擎切换改为**直通**（立即提交 + 发 `engineChanged`）；`_emit_pending` 发实算待定（去硬编码 `True`）；移除「生成迁移计划」按钮的可行性判定，交给 `_generate_plan` 给明确原因。
- **提交**：`f9b2d27`。回归钉：`tests/test_ui_execute_flow.py` ⑭⑮⑯⑰。

### D-002 · [Glue] `_cancel_current` 漏掉 `_stream_worker`，Esc 无法取消分块流式迁移 — 已修复
- **位置**：`app/ui/main_window.py:2279`（`_cancel_current`）
- **根因**：取消清单只遍历 `move/rollback/scan/index/plan/bitmap` 六个 worker，**遗漏 `_stream_worker`**。`closeEvent` 已包含它，但运行时按 Esc（`keyPressEvent`→`_cancel_current`）无法中止正在进行的 `BlockStreamWorker`。
- **修复**：在 `_cancel_current` 遍历元组中加入 `self._stream_worker`（其 `request_cancel`/`isRunning` 均受支持，见 `app/core/scanner.py`）。
- **提交**：待提交（与 D-003~D-008 同批）。

### D-010 · [Glue/UI · 中] 分块流式迁移的危险确认框漏传必填参数 `target_range` → TypeError 崩溃 — 已修复
- **位置**：`app/ui/main_window.py:620`（`_start_stream_move` 构造 `DangerConfirmDialog` 处）
- **根因**：v2 给 `DangerConfirmDialog.__init__` 增加了必填参数 `target_range: Tuple[int, int]`（无默认值，用于渲染「目标区间」行）。LBA 迁移（`main_window.py:2067` 传 `target_range=(start, end)`）与回滚（`main_window.py:2200` 传 `(0, 0)`）调用点都已补，唯独**分块流式调用点漏传**。用户点「开始分块流式迁移」→ 构造对话框时 `TypeError: __init__() missing 1 required positional argument: 'target_range'`，**流式引擎完全无法启动**。testflow 不驱动真实 UI，故此前未被发现。
- **修复**：流式无显式目标 LCN 窗口（落点由文件系统分配），与回滚一致传占位 `target_range=(0, 0)`。
- **回归钉**：`tests/test_ui_execute_flow.py` ⑱（`test_stream_move_dialog_receives_target_range`，记录型假对话框捕获构造关键字参、断言 `target_range` 存在且为 `(0, 0)`）。
- **提交**：待提交（本轮 UI 层复查发现并修复）。

---

## 已修复（1.0.0G · Debug 修复轮）

> 本轮由桌面 `LBA-Mover_Debug信息汇总.docx` 驱动，逐个销账。所有修复**均配回归钉并做了反向验证**
> （动态载入修前版本，确认钉按预期失败，防止钉是摆设）。

### D-003 · [Engine/IO · 中] `read_at` 无短读重试，可能静默截断数据块 — 已修复
- **位置**：`app/win32/file_io.py:112-148`（`read_at`）
- **修复**：循环读至累计满 `size`，续读用 `(ctypes.c_char * (n-got)).from_buffer(buf, got)` 缓冲视图切片推进（不重复分配整块缓冲）；遇真 EOF（`ReadFile` 返回 0 字节）抛 `api.Win32Error(api.ERROR_HANDLE_EOF, ...)`，而非静默返回短数据；`size <= 0` 早返回 `b""` 防死循环。沿用既有异常类型，未新造。
- **审查补记（原文档未记载）**：`read_at` 同时被用于**读暂存盘**（disk 缓存的 store 阶段，`file_io.py:211`）。旧实现在该路径会造成「源读短 → 暂存写短 → 目的再短」的**双重**静默损坏；改后该路径变为显式抛错。
- **回归钉**：`tests/test_file_io.py` —— `test_read_at_retries_until_full_size_on_short_reads`（chunk=1/7/512/4999 四种短读步长均读满且内容一致，断言 `calls > 1`）、`test_read_at_raises_on_premature_eof`（请求超文件长度与从 EOF 之后读均必抛且 `winerror == ERROR_HANDLE_EOF`；请求量恰等于文件长度时不误报）。
- **提交**：`dee0c71`。

### D-004 · [Engine/IO · 中] disk 缓存暂存文件仅用 `basename(dst)`，同名文件冲突 — 已修复
- **位置**：`app/win32/file_io.py:317-331`（新增 `_scratch_name`）+ `copy_stream:383-385`
- **修复**：`_scratch_name(dst_path)` = `sha1(normcase(abspath(dst_path)))` + `".stage"`；跨目录唯一、同一目标可复现（相对路径 / 大小写归一）、不含用户文件名（避免超长名与非法字符）；仍以 `.stage` 结尾，故 `scanner.py:1727` 既有收尾清理（`endswith(".stage")`）完全不受影响。`staged_copy` 的 finally 清理语义一字未改。
- **回归钉**：`test_scratch_name_unique_across_directories`、`test_copy_stream_disk_scratch_unique_per_directory`（两个不同目录下同名文件共用同一 `cache_dir`，实测走不同暂存路径、两个 dst 内容各自正确、收尾 `cache_dir` 为空）。
- **提交**：`dee0c71`。

### D-005 · [Engine/逻辑 · 低] 部分段失败的文件 `moved_files` 计数缺失 — 已修复
- **位置**：`app/win32/mover.py:151`（docstring）+ `:258-261`；配套 `app/core/scanner.py:82`（新增 `account_move_result`）、`:1399`（MoveWorker）、`:1645`（RollbackWorker）
- **修复**：`mover.execute_segments` 判定由 `not outcome.failures and not outcome.cancelled` 改为 `outcome.moved_clusters > 0`（该值只在 FSCTL 真正成功时累加，是唯一权威判据），于是「至少有一段搬成功」即计入，允许与 `failures` 并存（= 部分成功）。failures 清单 / cancelled 标志 / 逐段流水记录的产生逻辑一行未动。
- **关键配套发现（原文档未记载）**：UI 的「移动 N 个文件」来自 MoveWorker / RollbackWorker 的**自计数**，二者原本**根本没用** `result.moved_files`（`scanner.py` 原为 `else:` 分支内联 `+= 1`）⇒ **只改 `mover.py` 界面文案一个字都不会变**。故新增模块级唯一汇总口径 `account_move_result(outcome, result)`，两个 worker 统一改用它，且计入点位于任何 `break` **之前**（否则 `on_error=halt` 会连簇数一起吞掉本次成果）。
- **回归钉**：`tests/test_executor.py` 4 例（`test_partial_success_counts_file_and_keeps_failure`、`test_partial_then_cancel_counts_file`，外加**反向钉** `test_nothing_moved_never_counts_file`、`test_guard_rejected_file_never_counts_file` 防「修过头变虚报」）；`tests/test_scheduler.py` 2 例（`test_account_move_result_counts_partial_success`、`test_both_workers_use_the_shared_accounting_rule`）。
- **提交**：`834a159`。

### D-006 · [Glue/资源 · 低] 切换卷 / 回滚完成后未关闭 journal 句柄 — 已修复
- **位置**：`app/ui/main_window.py:2186-2208`（新增 `_release_journal`）、`:880`（`_on_volume_changed`）、`:2286`（`_on_rollback_completed`）
- **修复**：`_release_journal()` 先取旧句柄 → **立即**把 `_journal` / `_journal_path` 复位 → 再 `close()`。`MoveJournal.close()` 自身幂等且内部已吞 fsync 异常，外层再包一层 try，保证重复调用 / 已关闭情况都不抛。两处原先的裸 `self._journal = None` 改为调用它。
- **回归钉**：`tests/test_ui_execute_flow.py` —— `test_release_journal_closes_handle_and_clears_reference`（句柄真被关 + 引用与路径清空 + 幂等连调 3 次 + 内容已 fsync 落盘可读）、`test_release_journal_is_reachable_from_both_drop_paths`（两条路径都必须调用它、不许再裸置空）、`test_volume_changed_closes_the_open_journal`（功能钉：真切一次卷验证旧句柄被关；`storage.probe_volume_storage` 已替身，不碰真实设备）。变异：去掉 `journal.close()` → 前两条立刻 FAIL。
- **提交**：`560d03b`。

### D-008 · [Engine/性能 · 低] `staged_copy` 攒批合并带来 ~2× block 内存峰值 — 已修复
- **位置**：`app/win32/file_io.py:179-239`（新增 `MAX_MERGE_BYTES` / `_write_run`）+ `_flush_pending:250-257`
- **修复**：**限制单次合并长度**而非文档所列的「逐块分段写」—— 后者会把 1.0.0D 方案 D 的连续合并收益从 3 次写退回 5 次（本质是功能回退）。新增 `MAX_MERGE_BYTES = 8 << 20` 与 `_write_run`：连续段仍合并但单次合并受上限约束；段内单块已 ≥ 上限时**整块直写、不做 join 复制**；段内只有 1 块时直接写 `segment[0]`。落盘字节路径与 join 版完全一致，默认 8 MiB 远大于常规 block 故合并收益完整保留（实测仍 5 块 → 3 次 `WriteFile`）。要大改只需调 `MAX_MERGE_BYTES` 一个常量。
- **回归钉**：`test_write_run_splits_long_run_and_preserves_bytes`、`test_staged_copy_segmented_write_matches_joined_bytes`（cap=1MiB/3000/1 三档落盘均与源逐字节一致，并保留 `stats[3000] > stats[1MiB]` 的次数断言使其具备回归检出能力）。
- **提交**：`dee0c71`。

### D-009 · [Middle/Stream · 低] 流式崩溃残留 `.lbastream.tmp` 且下次不清理 — 已修复（含更严重变种）
- **位置**：`app/core/scanner.py:119`（新增 `STREAM_TMP_SUFFIX`）、`:122-185`（`resolve_stream_residue`）、`BlockStreamWorker.run():1782 / :1787`
- **修复**：在**动笔拷贝之前**调用 `resolve_stream_residue(src, tmp, recycle)`，两种情形分开处理：① **src 已不在**（源已入回收站、改名未及完成）—— tmp 是这份数据的**唯一幸存副本**，必须补完 `os.replace(tmp, src)` 恢复源文件，**绝不删除**；② src 仍在 —— tmp 只是中断产生的重复数据，回收（失败再直删）后继续。清不掉则抛 `OSError` 让 worker 跳过该文件，绝不带着未知内容继续写。
- **为何不把 tmp 挪进 scratch 目录**（原文档方案之一）：`os.replace` 必须同卷，而 `file_io.default_scratch_dir` 在卷根不可写时会回落 `%TEMP%`（可能跨卷）⇒ rename 直接失败；留在源目录才能保住「数据先落到卷尾空闲连续区、再 rename 回原名」这个腾空前部簇的关键语义。
- **原文档描述的一处偏轻**：当前 `file_io` 的 `preallocate_dst=True` 走 `CREATE_ALWAYS`（先截断再写），旧残留下半截**不会**真的被拼进新文件；**真正的问题**是「src 缺失时 `os.path.getsize(src)` 先抛 `FileNotFoundError` → 该文件被判失败，而那份 tmp 是用户数据的唯一幸存副本，恢复路径不存在且源目录空间一直占着」，本次一并管住。
- **回归钉**：`tests/test_auto_stream.py`（以 `app.win32.file_io` 替身跑真实 `BlockStreamWorker.run()`，替身记录每次 `copy_stream` 进入时 dst 是否已存在；不动用户回收站）—— `test_resolve_stream_residue_branches`（三形态，含「幸存副本不许被 recycle/删除」断言）、`test_stream_worker_clears_stale_tmp_before_copy`、`test_stream_worker_recovers_source_from_interrupted_rename`。变异：去掉 `:1787` 那行调用 → 后两条立刻 FAIL。
- **提交**：`f0ab203`。

### D-011 · [Engine/IO · 中] `write_at` 无短写重试 / 未校验写入长度（本轮审查新发现，D-003 的写侧孪生） — 已修复
- **位置**：`app/win32/file_io.py:151-197`（`write_at`）、`staged_copy:339-349`、`memory_copy:409-419`
- **发现经过**：修复 D-003 时连带发现写侧存在完全对称的漏洞 —— `write_at` 不校验 `done.value == len(data)`，短写（磁盘满 / 配额 / 驱动行为）会静默少写；`staged_copy` 与 `memory_copy` 也只把返回值静默累加进 `written`、不比对块长度。原有唯一保障是 `scanner.py:1705` 的整文件总量比对，但那要**跑完整文件后**才发现，且发生在 `recycle` 之前（不丢数据，却会留下「假成功 / 半截文件」的判定路径）。特别地 **`memory` 是 worker 默认模式**，故该缺口影响默认路径。
- **修复**：`write_at` 循环 `WriteFile` 直至写满 `len(data)`，续写用 `(ctypes.c_char * (n - written)).from_buffer(buf, written)` 视图切片推进；**快路径零额外内存**（首次直接写原始 `data` 字节串，只有真短写时才 `create_string_buffer` 建可写副本）；写入停滞抛 `api.Win32Error(api.ERROR_DISK_FULL, ...)`；`data` 为空早返回 `0`。`staged_copy` 与 `memory_copy` 的 store 分支统一改为「取返回值 → 与块长度比对 → 不符即抛 → 才累加」。
- **回归钉**：6 例，含 `test_write_at_retries_until_full_length_on_short_writes`（chunk=1/7/512/4999）、`test_write_at_raises_when_write_stalls`、`test_staged_copy_rejects_short_block_write`、`test_staged_copy_write_guard_keeps_join_equivalence_and_merge_counts`、`test_memory_copy_rejects_short_block_write`（并断言异常后 `os.remove` 仍成功 —— 句柄以 `FILE_SHARE_READ|WRITE` 打开且无 `FILE_SHARE_DELETE`，漏关则删除必 PermissionError，故该断言真实证明句柄已关）、`test_memory_copy_lossless_under_short_writes`。
- **反向验证彩蛋**：第 4 条钉在修前版本下的失败形态是 `Win32Error: ReadFile@0 未读满即 EOF（请求 2048 字节，实得 7 字节）` —— 旧 `write_at` 在持续短写下只往暂存盘写了 7 字节，随后被**读侧 D-003 的新校验**抓住。反向印证 D-003 与 D-011 是互相兜底的一套，缺哪一侧都会在另一处暴露或静默。
- **提交**：`fc627d0`（`write_at` + `staged_copy`）、`fd7248a`（`memory_copy` 补齐）。

---

## 非缺陷（已坐实）

### D-007 · [Engine/边界 · 中] `break_cycles` 中转空间分配边界 — **结论：非缺陷（虚报），代码一行未改**
- **位置**：`app/core/scheduler.py:450`（`ledger_relay.allocate(..., near=window.end_lcn)`）+ `app/core/packer.py:83`（`SpaceLedger.allocate`）
- **验证结论**：逐分支读透 `packer.py:103-141` 并穷举测试，原怀疑的两点**均不成立**：
  1. **`near` 超出 `total`** —— 三条分支只有「第 2 条：`end <= near` 的最高段、段尾对齐」会命中，其选择条件 `end - start >= clusters` ⇒ 必有 `end - clusters >= start`，返回值天然落在某个空闲段内，**不存在越界、也不需要显式 clamp**；放不下则诚实返回 `None` 且不动账本。
  2. **`near` 落在已分配段** —— ledger 只维护**空闲段** `_runs`，某区间一旦被 `allocate` / `take` 即从 `_runs` 消失，于是「包含该 near 的空闲段」不存在，第 3 条分支（`start < near < end`）永远不可能命中已消费区间 ⇒ 结果必然让到该段之外，**不存在重复租借**。
  3. **顺带坐实「中转区与目标窗口重叠」** —— `ledger_relay` = `ledger_out` = `SpaceLedger(analysis.evict_space)`，而 `evict_space = subtract_runs(free_runs, [(window.start, window.end)])`、`free_runs` 又已在 `scanner.py:931` 扣过 forbidden ⇒ 中转不可能落进窗口或安全禁区。
- **产物**：补 8 例边界钉锁住上述语义（`test_packer.py` 5 例，含 near 取 -1 / 段首 / 段中 / 段尾 / 超界穷举并断言两次分配互不相交；`test_scheduler.py` 3 例钉住真实调用点 `scheduler.py:450` 的行为）。**未修改 `packer.py` / `scheduler.py` 任何逻辑**。
- **过程插曲**：修复期间曾出现 `test_packer` 33 passed / 1 failed，系编写者**自己的 TDD 中间态**（初版期望值算错，写成 `allocate(25, near=1e9) == 10`，实则按上述语义放不下返回 `None`），重跑 34/0。它与 file_io 并行改动**无因果关系**（`tests/test_packer.py` 仅 import `app.core.models / ownership / packer / ranges / safety`，`app/core/packer.py` 无任何 `win32` / `file_io` 引用）。该次「失败」反而再次印证了「越界 near 既不返回 near 本身也不越界，放不下即返回 `None`」。
- **提交**：`2ec3e2d`（仅测试）。

---

## 本轮销账情况

- **1.0.0G 修复**：D-003、D-004、D-005、D-006、D-008、D-009、D-011（新发现）。
- **1.0.0G 证伪**：D-007（虚报，代码未改，仅补边界钉）。
- **此前已修复**：D-001、D-002、D-010。
- 至此，**缺陷日志中登记的项目已全部销账**（已修复或已坐实为非缺陷）。

---

## 审查范围与说明

- **审查对象**：`app/ui/main_window.py`（UI↔引擎接线层全量）、`app/win32/mover.py` / `file_io.py`（唯一写路径）、`app/core/scheduler.py` / `planner.py` / `safety.py`（规划与调度）、`app/core/block_stream.py`（流式内核纯逻辑）。
- **未覆盖**：`app/win32/mft_reader.py`、ScanWorker/MftScanWorker/IndexWorker/BitmapWorker 的**扫描通道内部实现**（MFT/句柄双通道、归属索引）、`app/core/mft.py`（直接 `$MFT` 解析）、`densify.py` 全部分块策略均为**潜在风险盲区**，建议后续 AI 单独排期审查。
  - （原条目中的「真机磁盘行为无法跑 `FSCTL_*` 实路径」一项**已被 1.0.0G 新增的 smoke 流部分覆盖**：smoke 的第 ④ 项 `app.win32.volume.list_volumes()` 已能在真实机器上走 FSCTL 实路径枚举卷（实测返回 9 个卷）；真正仍需真机复验的是 **FSCTL_MOVE_FILE 写路径本身**。）
- **已确认非缺陷**：`ledger_out` 中转空间排除禁区（见 D-007，已于 1.0.0G 坐实）；`block_stream.stream_schedule` 缓冲上界与流水线重叠逻辑正确（`simulate_peak_resident` / `has_pipeline_overlap` 可校验）；`mover.py` 写路径安全不变量（`guard.allow` + `policy` 双校验 + 路径未知拦截）完整；**`scanner.py` 五个引擎桥接 worker 的 `run()` 接线层（PlanWorker / MoveWorker / RollbackWorker / BlockStreamWorker / FeasibilityWorker）回查已确认完好**（见下方「引擎↔中间层桥接审计」）。
- **引擎↔中间层桥接审计（2026-10-24）**：逐一审视 `scanner.py` 五个 worker 的 `run()`，确认其作为「UI 信号 ↔ 引擎内核」的中间层接线**正确**：① 主窗口五个实例化点（`main_window.py:636/1551/1807/2094/2216`）传入的参数与 worker `__init__` 签名逐一吻合；② worker 把参数正确转交引擎内核（`planner.build_plan/build_range_plan`、`densify.auto_fill_tail/compact_range/zone_plan`、`packer.assess`、`mover.execute_task/rollback_segments`、`file_io.copy_stream/recycle`）；③ 发出的信号（`progress`/`file_result`/`completed`/`failed`/`finished`）与主窗口所连槽函数签名、载荷类型一致；④ 取消语义正确（PlanWorker/FeasibilityWorker 裸 `return` 依赖 `finished` 兜底；MoveWorker/RollbackWorker/BlockStreamWorker 在循环处 `break` + 补发 `completed`）；⑤ 写路径隔离不变（`app.win32.mover` 仅在 `MoveWorker`/`RollbackWorker.run()` 内按需 import）。另确认 `MoveTask.clusters` 为 `@property`（非陈旧字段），故 `MoveWorker` 进度分子与 R3 一致性校验分母均取真实段簇和，无「0 簇」假阴性。本层**未引入新缺陷**，仅补记 D-009（流式崩溃残留 `.lbastream.tmp`）。
- **写路径隔离不变量（1.0.0G 起可自动化验证）**：smoke 流第 ③ 项已把「import `app.ui.main_window` 后 `app.win32.mover` 不应出现在 `sys.modules`」固化为自动化检查，无需再靠人工静态审查。
