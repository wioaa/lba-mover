# 缺陷日志（DEFECT LOG）

> 本文件由「沉淀期与缺陷修复期」的代码审查生成，供**未来更高级别的 AI 模型**接手修复。
> 状态字段：
> - **待修复**：已确认/疑似存在缺陷，尚未修复；
> - **需深入验证**：定位到可疑区域，但未在真机/单测层面坐实，需进一步确认；
> - **已修复**：本轮已解决，留痕供追溯。
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

---

## 待修复

### D-003 · [Engine/IO · 中] `read_at` 无短读重试，可能静默截断数据块
- **位置**：`app/win32/file_io.py:110-118`（`read_at`）
- **描述**：`ReadFile` 返回后只取 `buf.raw[:done.value]`，未校验 `done.value == size`。当磁盘 `ReadFile` 返回**少于请求字节**（压缩/Sparse 文件、某些驱动或边界条件）时，该块被**静默截断**，`write_at` 仅写入短数据 → 分块流式迁移产生**数据不一致**。流式迁移被定位为「更安全的用户态替代」，此缺陷直接动摇其正确性前提。
- **触发条件**：对压缩/稀疏文件、或任何触发 ReadFile 短读的路径跑分块流式迁移（memory / disk 两种缓存模式都经 `read_at`）。
- **修复建议**：循环读直到 `done == size` 或到达 EOF（返回短读即截断），并对「非 EOF 的短读」显式报错而非静默继续。

### D-004 · [Engine/IO · 中] disk 缓存暂存文件仅用 `basename(dst)`，同名文件冲突
- **位置**：`app/win32/file_io.py:355`（`copy_stream` → `scratch = basename(dst)+".stage"`）
- **描述**：暂存盘文件名只取目标文件 basename，不区分目录。**不同目录的同名文件**（如 `D:\a\photo.jpg` 与 `D:\b\photo.jpg`）会复用同一 `.stage` 文件。单进程内 `BlockStreamWorker` 顺序处理且 `finally` 内 `os.remove(scratch)` 已清理，故普通顺序迁移不易触发；但**崩溃残留 `.stage` 复用**、或**外部共享 `cache_dir`**、或未来改为并发时，存在互相覆盖 / 数据错乱风险。
- **触发条件**：流式迁移多个不同目录同名文件；或上一轮异常退出遗留 `.stage` 后再次迁移同名文件。
- **修复建议**：暂存文件用完整路径哈希或 `uuid` 命名（如 `<sha1(dst)> .stage`），并在 `finally` 后安全清理目录。

### D-005 · [Engine/逻辑 · 低] 部分段失败的文件 `moved_files` 计数缺失
- **位置**：`app/win32/mover.py:249-251`（`execute_segments`）
- **描述**：`moved_files` 仅在 `not outcome.failures and not outcome.cancelled` 时置 1。一个文件若**部分段成功、部分段失败**，`moved_clusters > 0` 但 `moved_files == 0`，UI「移动 N 个文件」少算。功能上回滚基于 journal 的 `ok=True` 段重建，**不影响可恢复性**，仅统计口径不一致。
- **触发条件**：构造一个多 extent 文件，使其某段目标命中安全禁区（`assert_target_allowed` 拒绝）。
- **修复建议**：按「是否有任意段成功移动」计 `moved_files`，或单独统计「部分失败」状态。

### D-006 · [Glue/资源 · 低] 切换卷 / 回滚完成后未关闭 journal 句柄
- **位置**：`app/ui/main_window.py:875`（`_on_volume_changed`）、`:2260`（`_on_rollback_completed`）
- **描述**：两处在把 `self._journal` 置 `None` 前**未调用 `self._journal.close()`**（仅在 `_on_move_completed` 正常完成时有关闭）。切换卷或回滚完成后，`MoveJournal` 文件句柄泄漏至 GC 回收。
- **触发条件**：执行迁移 → 切换卷（需在非 busy 态）；或回滚完成后。
- **修复建议**：置空前显式 `self._journal.close()`，并将 `_journal_path` 一并清空。

### D-007 · [Engine/边界 · 中 · 需深入验证] `break_cycles` 中转空间分配边界未坐实
- **位置**：`app/core/scheduler.py:450`（`ledger_relay.allocate(..., near=window.end_lcn)`）+ `app/core/packer.py:83`（`SpaceLedger.allocate`）
- **描述**：relay 中转段的目标 LCN 由 `ledger_relay.allocate` 给出。已确认调用方传入的 `ledger_relay` 即 `ledger_out`，而 `ledger_out` 由 `analysis.evict_space`（`window` 内空闲区且已 `subtract_forbidden(policy.forbidden_runs())`）构造，故中转**不会落入安全禁区**（执行期 `assert_target_allowed` 也会兜底拦截，不损坏数据）。但 `allocate` 内部对 `near` 越界（超出 `total_clusters`）的 clamp 行为、**中转区与目标窗口/已分配区重叠**的处理尚未在单测层面坐实，需确认其边界。
- **触发条件**：含依赖环、且空闲区紧邻目标窗口边缘的卷布局。
- **修复建议**：核查 `SpaceLedger.allocate` 边界 clamp 与重叠排除，补单测覆盖「near 超出 total / near 落在已分配段」两种情形。

### D-008 · [Engine/性能 · 低] `staged_copy` 攒批合并带来 ~2× block 内存峰值
- **位置**：`app/win32/file_io.py:185-196`（`_flush_pending` 的 `b"".join`）
- **描述**：disk 缓存模式下，攒批先把各 block 字节存进 `pending` dict，flush 时 `b"".join` 拼成一段连续 buffer 再 `write_at`。峰值内存约为 1 个 block（pending 蓄满）的 **~2 倍**（dict 中的分块 + join 出的整段）。大 `buffer_blocks` 下峰值翻倍，曾记为「待评估」。
- **触发条件**：disk 缓存模式 + 大 `buffer_blocks` 搬运大文件。
- **修复建议**：用 `WriteFile` 的分段写入避免整段 join，或限制单次合并长度。

### D-009 · [Middle/Stream · 低] `recycle(src)` 与 `os.replace(tmp, src)` 之间崩溃会遗留 `.lbastream.tmp` 且下次不清理
- **位置**：`app/core/scanner.py:1693-1709`（`BlockStreamWorker.run`）
- **描述**：每个源文件先在**自身目录**生成 `tmp = src + ".lbastream.tmp"`，`copy_stream` 完成后 `recycle(src)` 把源移入回收站，再 `os.replace(tmp, src)` 改回原名。若在 `recycle(src)` 成功之后、`os.replace` 之前的极窄窗口内进程被杀/断电，源已入回收站、`.lbastream.tmp` 残留在源目录。而结束清理逻辑（`scanner.py:1721-1734`）只扫 `scratch_dir` 里的 `.stage`，**不会清理源目录的 `.lbastream.tmp`**。下次若再次选中同一文件，`copy_stream(preallocate_dst=True)` 会向一个已存在的 tmp 继续写入，可能被旧残留污染 / 行为不确定。
- **触发条件**：流式迁移执行中在「源已进回收站、tmp 未改名」窗口内崩溃或断电。
- **修复建议**：把 `.lbastream.tmp` 改放到与 `.stage` 同一临时目录（或启动时扫描选中文件目录、提示/清理残留 `.lbastream.tmp`）；结束清理同时删除源目录的 `.lbastream.tmp`。

---

## 审查范围与说明

- **审查对象**：`app/ui/main_window.py`（UI↔引擎接线层全量）、`app/win32/mover.py` / `file_io.py`（唯一写路径）、`app/core/scheduler.py` / `planner.py` / `safety.py`（规划与调度）、`app/core/block_stream.py`（流式内核纯逻辑）。
- **未覆盖**：`app/win32/mft_reader.py`、ScanWorker/MftScanWorker/IndexWorker/BitmapWorker 的**扫描通道内部实现**（MFT/句柄双通道、归属索引）、`app/core/mft.py`（直接 `$MFT` 解析）、`densify.py` 全部分块策略、**真机磁盘行为**（本环境无真实 NTFS 卷，无法跑 `FSCTL_*` 实路径）均为**潜在风险盲区**，建议后续 AI 单独排期审查。
- **已确认非缺陷**：`ledger_out` 中转空间排除禁区（见 D-007）；`block_stream.stream_schedule` 缓冲上界与流水线重叠逻辑正确（`simulate_peak_resident` / `has_pipeline_overlap` 可校验）；`mover.py` 写路径安全不变量（`guard.allow` + `policy` 双校验 + 路径未知拦截）完整；**`scanner.py` 五个引擎桥接 worker 的 `run()` 接线层（PlanWorker / MoveWorker / RollbackWorker / BlockStreamWorker / FeasibilityWorker）本轮回查已确认完好**（见下方「引擎↔中间层桥接审计」）。
- **引擎↔中间层桥接审计（2026-10-24）**：逐一审视 `scanner.py` 五个 worker 的 `run()`，确认其作为「UI 信号 ↔ 引擎内核」的中间层接线**正确**：① 主窗口五个实例化点（`main_window.py:636/1551/1807/2094/2216`）传入的参数与 worker `__init__` 签名逐一吻合；② worker 把参数正确转交引擎内核（`planner.build_plan/build_range_plan`、`densify.auto_fill_tail/compact_range/zone_plan`、`packer.assess`、`mover.execute_task/rollback_segments`、`file_io.copy_stream/recycle`）；③ 发出的信号（`progress`/`file_result`/`completed`/`failed`/`finished`）与主窗口所连槽函数签名、载荷类型一致；④ 取消语义正确（PlanWorker/FeasibilityWorker 裸 `return` 依赖 `finished` 兜底；MoveWorker/RollbackWorker/BlockStreamWorker 在循环处 `break` + 补发 `completed`）；⑤ 写路径隔离不变（`app.win32.mover` 仅在 `MoveWorker`/`RollbackWorker.run()` 内按需 import）。另确认 `MoveTask.clusters` 为 `@property`（非陈旧字段），故 `MoveWorker` 进度分子与 R3 一致性校验分母均取真实段簇和，无「0 簇」假阴性。本层**未引入新缺陷**，仅补记 D-009（流式崩溃残留 `.lbastream.tmp`）。
