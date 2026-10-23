# 1.0.0D 四 commit 独立验证报告（QA）

> **2024-xx 追加**：第 5 个 commit `e961eed`（方案 A 日志批量刷盘 + 方案 D 流式 disk 缓存写合并/同卷默认暂存）已并入验证，见文末「§6 e961eed 专项」。

| 项 | 内容 |
|---|---|
| 报告人 | QA 严过关 |
| 对象 | `c9e9b75`（版本号）/ `0aae821`（界面性能）/ `9a19d64`（确认后提交流程）/ `733fe8c`（可行性移线程） |
| 方式 | 证伪导向：diff 审查 + 自写离屏回归 `qa_work/d1_ui_regression.py`（非复用工程师用例）+ 独立性能基准 |
| 约束 | 未修改任何项目文件；脚本与原始输出在 `qa_work/d1_*` |
| 工程师自报 | testflow 294/0、`_clip_extent` 668×、band 缓存 6649×、pending 信号恰好一次、写路径隔离恒成立 |
| **结论** | **可以放行，但有 1 个 P1 必须修 + 4 个 P2 登记**；性能声称量级成立，一个绝对数字对不上 |

---

## 1. 实测数字（我跑出来的）

```
testflow 全量：302 passed / 0 failed / 36.5s   ← 工程师口径 294/0（多 8 例，记录在案）
离屏回归（自写 52 项断言）：50 PASS / 2 FAIL（2 项同源，见 B-1）
写路径隔离：实例化 MainWindow 后 'app.win32.mover' not in sys.modules → MOVER_CLEAN
版本号三处：display_version()=='1.0.0D' / version_info.txt ✓ / CHANGELOG ✓
FeasibilityWorker：真实 QThread 完成 1 结果 + seq 一致；run 前取消 → 0 结果；bits 类型 bytes ✓
拖选：release 恰好 1 次；单击/双击/右键 0 次；越界钳制后仍恰好 1 次
性能：find_blockers 10万×5万 = 431ms（真线性参考外推 ~242s，加速 ≈ 562×，正确性 0 差异）
      band 缓存命中 5088×（首绘 3.55ms → 0.0007ms）
```

---

## 2. Bug 清单

### B-1（P1，必须修）执行模式三选确认：两个动作按钮全部等价于「取消」

`ChoiceDialog._pick` 存的是**按钮文字**，而调用方比较的是**代号**：

```
app/ui/dialogs.py:524   self._codes = [code for code, _role in choices]   # ← 是文字
app/ui/dialogs.py:528   self.choice = self._codes[index]                  # choice='先应用再执行'
app/ui/main_window.py:1739  if dialog.choice == "apply":     ← 永远 False
app/ui/main_window.py:1748  if dialog.choice != "proceed": return  ← 永远 True → return
```

实测（Part C，直接实例化对话框并点击）：

```
点「先应用再执行」 → choice='先应用再执行'
点「按现有计划执行」 → choice='按现有计划执行'
```

后果：**配置有待定修改时，用户在执行确认框里点哪个动作都没反应**，「按现有计划执行」这个逃生门实际不可用。失败安全（不会带错参数写盘），且用户可以先点侧栏「应用配置」绕过，故定 P1 而非 P0。

**建议修法（二选一）**：
- `ChoiceDialog` 改为 `choices=[(code, text, role)]` 三元组，`_codes` 收代号；
- 或调用方改为按**序号**判断：`_pick(0)→choice='先应用再执行'` 时比较文字常量（脆弱，不推荐）。
另加一条回归钉：`assert ChoiceDialog(...).choice == "apply"`（构造后 `_pick(0)`）。

### B-2（P2）`modeChanged` 信号已死

`target_panel._on_mode_changed`（1.0.0D 改动）不再发 `modeChanged`，只发 pending。全仓无其它发射点，主窗口也没连接它 —— 信号留着但永远不发。若未来有人接上会静默失效。建议：删掉信号或改由 `commit_pending` 按差异发出。

### B-3（P2）`_stream_worker` 不在 closeEvent 取消清单里

`main_window.py:2034-2045` 的取消清单含 7 个 worker，独缺 `_stream_worker`（BlockStreamWorker，1.0.0C 引入）。关闭窗口时正在跑的分块流式搬迁不会被 `request_cancel()`/`wait()`。属 1.0.0C 遗留，非本批引入，但「closeEvent 取消清单是否完整」的答案是不完整。

### B-4（P2）discard 后有一次瞬时的 pending 闪烁

`TargetPanel.discard_pending` 在 `_guard = False` **之后**调用 `_sync_mode_visual()`；已提交模式为 quick 时它走 `_recompute_quick()` → `_emit_pending()` → 发出一次 `pendingChanged(True, n)`，随后函数末尾再发 `(False, 0)`。实测信号序列多一对假脉冲，待定行会闪一帧。修法：`_sync_mode_visual()` 挪回 `_guard=True` 区间内，或给 `_emit_pending` 加 guard 检查（与 io_panel 对齐——io_panel 的 `_emit_pending` 有 guard 检查，target_panel 没有，两边不对称）。

### B-5（P2）`find_blockers` 绝对耗时与"17ms"声称对不上（场景相关，非错误）

我的独立基准（10 万 extent × 5 万 run，**100% 命中、产出 65000 个 blocker**）：431ms。工程师口径 11.5s→17ms。二分实现的复杂度是对的（真·线性参考在同一场景外推 ~242s，**加速 ≈ 562×，与声称 668× 量级吻合**；二分与线性在 300 样本上逐段比对 **0 差异**）。17ms 应来自低命中场景。建议他在 `tests/test_blockers_perf.py` 里标注基准场景的命中率，否则"17ms"不可复现。

### B-6（P3）pending 记忆键用 `id(snapshot)`

`_compute_feasibility_now` 的 signature 含 `id(snapshot)`。旧快照释放后新对象可能复用同一地址，理论上可造成"换了位图但签名相同 → 跳过重算"。概率低（重扫后 start/end/selected 大概率也变），登记备查。可用 `signature` 里加 `snapshot.bits 的 len+首尾若干字节哈希` 替代。

### B-7（P3）两处文档/口径小问题

- `TargetPanel.pending_count` 文档写"变更组数（目标区间/规划策略/保留区）"三组，实现只分 2 组（策略并入区间组）——行为对，文案错。
- quick-tail 模式的 `_quick_window()` 预览用 `reserve_tail_clusters()`（**已提交**值），尾部保留区控件待定时预览不跟随。与 `_live_reserve_mb` 的存在意图相悖，属预览不一致（P3）。

### 观测差异（非 bug，记录）

「pending 信号恰好一次」：`set_window_committed`（拖选直通）实际发 pendingChanged **两次**（True→False，进+出暂存态）。状态机正确、净效果正确；若工程师的口径是"每次操作恰好一次"，拖选路径不满足。

---

## 3. 确认无问题的部分（附判定依据）

| 审查点 | 结论 | 依据 |
|---|---|---|
| **committed/live 语义泄漏** | **无泄漏，语义判定为正确** | 逐调用点核对：`_generate_plan`（strategy/window/reserve/profile 全 committed）、`_policy()`、`_compute_feasibility_now`（committed window）、`_refresh_target_visual`（committed window + committed reserve clusters）、`io_panel.set_window_clusters`、执行路径 1541/1593/1612/1627 全 committed；live 值只出现在**面板自身回显**（`_live_window` 徽章、`_live_buffer_mb` 生效值预览、`pending_engine` 提示）。**判定：可行性/规划喂的是已提交区间 —— 用户改了没应用时，可行性显示的是已提交区间；这正是"当前计划始终基于已提交参数"的正确实现。** 待定修改的回显职责由面板徽章与摘要卡待定提示承担，分工清晰 |
| 拖选直通 vs spinbox | 行为确实不同且正确 | `set_window_committed`：windowChanged 恰好 1 次 + 零待定；spinbox 路径：windowChanged 0 次 + 进暂存（A7 实测） |
| 拖选 release-only 边界 | 全对 | 单击 0、双击 0、右键 0、界内拖选 1 次、界内拉开后越界 1 次且越界端钳制（E1–E5）。注：**直接按下即出界**（无界内移动）为 0 次 —— 零宽区间不发，正确 |
| `_sync_buttons` 记忆化 | 覆盖完整 | 键 `(文案, 角色, 可用, _has_pending)` 恰好完全决定三处写入（setText/set_role/setEnabled）；全仓 `_action_button` 只经 `_apply_action_button` 写（grep 证实）；流式引擎早退路径同走该函数，无双写 |
| 计划失效链（三选确认的"先应用再执行"） | 链路本身通 | commit → windowChanged/profileChanged → `_on_target_changed`/`_on_profile_changed` → `_plan_stale=True`（H7 实测 True）；plan_stale=True 时 `_on_primary_action` 走 `_generate_plan` 不会执行旧计划。**只因 B-1 该分支不可达** |
| busy 期间不可触发对话框 | 对 | `_on_primary_action` 首行 busy 早退（H6 实测）；应用/放弃按钮 busy 禁用（H5） |
| Ctrl+Enter clean 时无效 | 对 | `_apply_pending` 首行 `_has_pending` 早退（H2） |
| 改 A→commit→改 B，dirty 只含 B | 对 | A4：pending_count==1（只保留区组） |
| discard 未 dirty | 安全 no-op 零信号 | A6 |
| FeasibilityWorker 线程安全 | 对 | `bits` 为不可变 `bytes`（`bitmap.py:111 bytes(bits)`）；未把 QImage/QWidget 传入线程（构造参数全是 bytes/tuple/list/entry）；`request_cancel` 在 free_runs 计算前后各查一次；`closeEvent` 有 `wait(2000)`（assess 有界，够用）；seq 代次丢弃 + rerun 标记收敛（最多一次冗余重算，无泄漏循环）；`FeasibilityWorker.run` import 仅 `app.core` / `app.core.models`（AST 证实，D2） |
| 写路径隔离 | 恒成立 | 子进程离屏实例化 MainWindow → `MOVER_CLEAN`（D1） |
| band 缓存 | 失效覆盖正确 | `_build_band_image` 只依赖 `self._density`（grep 证实），密度只在 `set_volume` 变化且有 invalidate；resizeEvent 清缓存；主题在键内。实测命中 5088× |
| 扫描批量插入 | 对 | 64 条或 100ms 冲刷；completed/failed 先 flush 再替换，无丢行窗口 |
| 版本号 | 三处对齐 | D3 |

---

## 4. 测试覆盖缺口

1. **三选确认无任何用例**：302 个用例里没有覆盖 `ChoiceDialog` 的 choice 值与 `_start_move` 三个分支 —— 恰好是 B-1 藏身处。建议补：`_pick` 后断言 choice 值 + 一个假 `_start_move` 走三分支。
2. **committed/live 交叉态无用例**：没有"改了不应用 → `profile()`/`window()` 必须仍旧值"的钉子。我 A2/B1/B2 的断言可直接搬进 tests。
3. **closeEvent 取消清单完整性无用例**：建议加"枚举 MainWindow 上所有 QThread 属性，断言每个都出现在取消清单"。
4. **性能基准缺场景标注**：`test_blockers_perf.py` 未写明命中率，导致绝对数字不可复现（B-5）。
5. **拖选越界**（真实抓取路径）在 offscreen 下难以合成，我的验证用的是直投事件，等价但建议真机手测一次。

---

## 5. 放行判断

**修复 B-1 后可放行**（其余 P2/P3 不阻塞 UI 集成）。B-1 是一行级修复 + 一条回归钉的事，但只要它存在，"确认后提交"这条新交互链的实际可用性为零。

---

## 6. e961eed 专项（方案 A 日志批量刷盘 + 方案 D 流式 disk 缓存写合并）

取证：diff 审查 + 自写脚本 `qa_work/d2_e961_check.py`（23 项断言全过，输出 `d2_output.txt`）+ testflow **305 passed / 0 failed**（40.2s，含本 commit 新增用例）。

### 6.1 逐项结论（主理人指定 5 点）

| # | 审查点 | 结论 | 依据 |
|---|---|---|---|
| ① | journal 批量 fsync 崩溃窗口语义 | **自洽** | 「进程崩溃不丢行」用**外部句柄可观测验证**：31 行未 fsync 时另一句柄立即可读全部 31 行（3.2，flush→页缓存）；断电窗口 = 未 fsync 尾部 ≤32 行/2s，**只影响回滚清单完整性、不影响数据**（FSCTL 已完成）——文档与实现一致 |
| ① | `force_fsync()` 调用点完整性 | **闭环成立** | `MoveWorker.run` 主循环包在 try/except/**共同出口**结构里：except 只把异常记进 failures 不提前 return，全部路径（正常/取消/halt/8 连败/执行期异常）汇入底部 `force_fsync_journal` → `completed.emit`（scanner.py:1364 附近）。4 个提前 return 的 failed 路径（无任务/卷身份/影子位图/写句柄）在本次 run **从未写过 journal**（写操作全在主循环内），无需刷盘。跨 run 残留由 RollbackWorker 开头兜底（见②） |
| ② | `RollbackWorker.run` 开头 fsync 位置 | **正确** | 唯一在 fsync **之前**的提前 return 是「没有可回滚的段」——该路径无回滚动作、无需钉盘；fsync 之后的所有失败路径（重读位图失败等）均已先落盘，只会更安全 |
| ③ | `_contiguous_runs` 乱序/跳号 | **不会写错偏移** | 写偏移取 `blocks[run[0]].offset`（首块），块布局线性（`blocks[i+1].offset == blocks[i].offset + length`，schedule 静态生成）恒成立；pending 经 `sorted()` 后分组，各 run 独立按各自首块偏移写。**实测注入乱序 load 序（两两交换）+ 跳号 + 跨 flush 边界（buffer=3、17 块非整除）**：dst 逐字节等于 src（2.2/2.6），写总量恰 2×文件无放大无丢写（2.4/2.6b），合并确实生效（6 次 > 单块的写，最长 12KB） |
| ④ | `default_scratch_dir` 回落 | **对** | 卷根探测（写 `.write-probe` 后删）失败 → `%TEMP%\lba-scratch`，makedirs 失败也不抛；实测两种路径（4.1/4.2/4.3） |
| ④ | 「scratch 句柄未关就 os.remove」旧缺陷修复 | **属实** | diff 对比：旧版 `os.remove(scratch_path)` 在 close 之前（try 内），新版移到 `finally: close(src/scratch/dst)` **之后**（file_io.py，注释所述 Windows share=0 删除失败的场景成立）；实测 stage 文件成功清理（2.5/4.4b） |
| ⑤ | 空 cache_dir 整链路 | **自洽** | 双重兜底：`BlockStreamWorker` 用首可搬文件解析默认目录并负责目录清理（`owned_scratch`）；`copy_stream` 自己也有 `if not cache_dir: default_scratch_dir(src_path)` 兜底（entries 全不可搬时 first=None 仍正确）。实测两次空 cache_dir 全链路内容逐字节正确（4.4/4.5） |

### 6.2 新发现问题

| # | 级别 | 位置 | 问题 | 复现/证据 |
|---|---|---|---|---|
| C-1 | **P2** | `app/win32/file_io.py`（staged_copy 异常路径）+ `scanner.py` BlockStreamWorker 收尾 | **进程被杀/断电时 stage 文件残留且无人清理**：`os.remove(scratch_path)` 只在正常路径执行（异常时第一个 finally 关句柄后异常继续传播，remove 不可达）；`BlockStreamWorker` 的 `os.rmdir(scratch_dir)` 只删**空**目录 → 有残留文件的默认暂存目录永久保留。重试同名 dst 会覆盖复用同名 stage（有界），但**不同 dst 的残留会持续积累在卷根隐藏目录**。活证据：`C:\.lba-tmp-scratch\o.bin.stage`（05:02:50 生成，早于 commit 05:06:35，开发期手工运行残留，至今无人清理） | 杀掉运行中的流式搬迁 → 查看卷根 `.lba-tmp-scratch` |
| C-2 | P3 | `app/core/scanner.py` BlockStreamWorker 默认目录解析 | 默认暂存目录取**首个可搬文件**的卷根 —— 单卷应用正确；若未来 entries 跨卷，其余文件的暂存落在别的卷：功能正确但"同卷"优化失效且无提示。登记备查 | 静态 |
| C-3 | P3 | `app/core/journal.py::append` | 批量窗口内 `record` 抛异常（如 journal 已 close）会被 MoveWorker 的 except 捕获记为执行期异常 —— 行为正确，但该异常发生在 `handle.write` 之后时**计数器/缓冲一致性**依赖 `_closed` 检查在前（当前顺序正确）。无缺陷，仅登记审查过 | 静态 |

### 6.3 e961eed 放行判断

**通过，可与前 4 个 commit 一起放行**（B-1 修完的前提下）。C-1 建议在下一批处理：把 `os.remove(scratch_path)` 挪进 finally，或 BlockStreamWorker 收尾改为「删目录内 `.stage` 文件 → 再 rmdir」。

---

## 7. 65c9f86 复核（B-1 修复 + 附带项）

取证：`qa_work/d3_65c9f86_check.py`（14 项断言全过，输出 `d3_output.txt`）+ testflow **307 passed / 0 failed** + ownership 计时用例连跑 3 次稳定。

### 7.1 B-1 修复：正确，且钉子经反向验证有效

- 正向：`_pick(0/1/2)` → choice == `'apply'/'proceed'/'cancel'`；未点击为 None；按钮渲染用第二列文字；main_window 比较值对接。
- **反向验证**（monkeypatch 模拟旧写法，不改项目文件）：模拟"旧 `_pick` 存文字"→ `test_choice_stores_code_not_text` 断言失败（应存 'apply'，实际 '先应用再执行'）；模拟"旧 `_codes` 退化为文字列"→ `test_choice_codes_match_choices` 失败。**两个钉子都能防住 B-1 回归。**

### 7.2 ⚠ 流程问题：B-3/B-6 修复未提交，钉子文件缺失

- `git status` 显示 `app/ui/main_window.py`、`app/ui/target_panel.py` 处于**已修改未提交**状态；B-3（closeEvent 补 `_stream_worker`）与 B-6（`_feasibility_memo_snapshot` 强引用防 id 复用）**不在 65c9f86 的 diff 里**（该 commit 仅 6 文件、main_window 仅 6 行 choices 改动）。工程师自述"65c9f86 另含 B-3 钉子"与 commit 事实不符。
- 更关键的：closeEvent 新注释引用 `tests/test_worker_lifecycle.py`（AST 扫描钉住"所有 _x_worker 属性必须在取消清单"），**该文件不存在** —— 钉子是空头支票。B-3 的代码修复本身正确且已在工作区生效（list 现在 8 个 worker），只差提交 + 把钉子文件补出来。
- B-4 同样分两半：`_emit_pending` 补 guard 在 65c9f86 ✓；`discard_pending` 视觉同步段包 guard 在工作区。两半都实测有效（discard 全程无 (True,*) 假脉冲；`_emit_pending` 实算 dirty，clean 时偶发调用发 (False,0)；guard 外补刷 quick 结果标签正常；正常改值路径未被 guard 误伤）。
- B-6 的强引用方案比我的"签名加哈希"建议更轻，认可；`self._feasibility_memo_snapshot is snapshot` 与签名比对并列，语义正确。

### 7.3 ownership 计时断言"三轮取最优"：**认可**

理由：该断言的备择假设是"线性扩展"（ratio ≈ 10），不是卡绝对速度；计时噪声只会单向拉长时间，**min-of-3 是对系统噪声最稳的估计量**（最优值最接近真实成本）。残余风险是"掩盖间歇性真实退化"，但那需要三轮噪声同向且幅度跨过 3× 阈值，概率可忽略；且同文件已有 500ms 绝对预算兜底（test_blockers_perf）。实测连跑 3 次 8 passed / 0 failed。

### 7.4 本轮小结

| 项 | 状态 |
|---|---|
| B-1（P1） | ✅ 修复 + 钉子反向验证有效（65c9f86） |
| B-2 死信号 | ✅ `modeChanged` 全仓 0 残留（65c9f86） |
| B-3 closeEvent | ⚠ 修复在工作区**未提交**，且引用的钉子文件 `tests/test_worker_lifecycle.py` 不存在 |
| B-4 假脉冲 | ✅ 两半均验证生效（一半在 65c9f86、一半在工作区） |
| B-5 性能口径 | ✅ 已标注 50% 命中率并引用 QA 独立数字 |
| B-6 id 复用 | ⚠ 强引用修复在工作区**未提交** |
| B-7 口径 | ✅ pending_count 2 组注释（65c9f86） |
| testflow | 307 passed / 0 failed |

**放行判断更新**：B-1 已闭环；**请工程师把工作区改动提交**（B-3/B-6/B-4 后半），并补齐 `tests/test_worker_lifecycle.py` 钉子文件 —— 在此之前 B-3 处于"代码已改、防护未落地"状态。

---

## 8. 收口（f5d94c3 / b874dd2 复核 + 最终放行结论）

取证：`qa_work/d3_65c9f86_check.py`（对 HEAD 重跑全过）、`tests/test_worker_lifecycle.py`（2/2 + 反向验证）、`qa_work/d4_b874dd2_check.py`（8 项全过，含 C-1 钉子反向验证）、`d2_e961_check.py` 回归全过、testflow **309 passed / 0 failed**。

### 8.1 f5d94c3（B-3/B-4 后半/B-6/B-7②）

- B-3：closeEvent 清单 8 个 worker 全覆盖；`tests/test_worker_lifecycle.py` 钉子**真实有效**——反向模拟"清单抹掉 `_stream_worker`"判 missing 命中；AST 扫描的 `_KNOWN_WORKERS` 集合与 MainWindow 全部 `_x_worker` 属性一一对应，新增 worker 时钉子①自检会提醒同步集合。
- B-4 后半（discard 视觉同步包 guard + dirty 实算）：d3 脚本 3.1–3.5 全过，无假脉冲、无误伤。
- B-6：memo 强引用已在 HEAD。
- B-7②：quick-tail 预览改 `_live_reserve_mb()`，与 `_live_*` 口径一致 ✓。
- 600ms 预算放宽合理（QA 实测 100% 命中 431ms，留有余量）。

### 8.2 b874dd2（C-1）

- 修法正确：`os.remove` 进 finally（异常路径清现场、不掩盖原异常）；worker 收尾先删目录内全部 `.stage` 再 rmdir，非 stage 文件不动。
- **钉子反向验证**：内联复刻 e961eed 旧写法 + 注入 dst 写入失败 → stage 残留（缺陷复现）→ `test_staged_copy_removes_stage_on_failure` 断言失败（变红）；修后版本同场景 stage 被清、异常原样上抛。worker 收尾钉子用预置 stale stage + 外人文件实测：残留被清、外人文件不动、目录非空保留。
- 实证残留 `C:\.lba-tmp-scratch\o.bin.stage` 已随修复清理（目录已不存在）。

### 8.3 最终放行结论

**1.0.0D 引擎与界面层全部 7 个 commit（c9e9b75 / 0aae821 / 9a19d64 / 733fe8c / e961eed / 65c9f86 / f5d94c3 / b874dd2，共 8 个）验证完毕，QA 发现的全部问题（1 P1 + 2 P2 + 5 P3）均已闭环且回归钉经反向验证有效。testflow 309 passed / 0 failed。**

**放行到下一阶段（UI 集成 / 发布流程）：通过。**

附注（不阻塞）：① 前一阶段 v2 引擎报告 `docs/QA-REPORT-v2-engine.md` 的 5 项引擎级问题（陈旧位图/属性列表空壳/relay 死代码/path_known/decode 上界）与本报告相互独立，若尚未修复应在进入真实执行前闭环；② `_feasibility_memo_snapshot` 强引用在多卷缓存场景需重估（单卷无虑）。

---

## 9. 800f4a4 复核（「先应用再执行」接续执行修复）

取证：`qa_work/d5_resume_flag_check.py`（14 项断言全过，输出 `d5_output.txt`）、`tests/test_ui_execute_flow.py`（6/6）、testflow **315 passed / 0 failed**、离屏冒烟 `MOVER_CLEAN` + `resume_flag_init=False / busy=False / _plan_worker=None`。

### 9.1 主理人指定 5 点

| # | 审查点 | 结论 |
|---|---|---|
| 1 | **标志悬挂** | **失败（P2）**：3 条真实路径悬挂，另 1 条边缘路径。详见 9.2 |
| 2 | **接续仍弹确认框** | **通过**：`_on_plan_ready` 中 `_set_busy(False)`/`_plan_worker=None` 在函数最顶部、resume 块在末尾 → 接续时 `busy=False`、`plan_worker=None`（实测 C1/C2）；DangerConfirmDialog **恰好实例化 1 次**（C3） |
| 3 | **不二次执行** | **通过**：标志在 resume 块入口即复位；连续三次 `_on_plan_ready` 只接续 1 次（D1） |
| 4 | **钉子反向验证** | **部分有效**：旧 apply 写法下钉子①（apply 置标志+重规划）与钉子⑥（静默 return 提示）变红，②③④⑤仍绿 —— 覆盖了本次两处实质改动，**但未覆盖悬挂路径** |
| 5 | testflow + 冒烟 | **通过**：315/0；`MOVER_CLEAN`；初始 `resume_flag=False / busy=False / plan_worker=None` |

### 9.2 缺陷 1（P2）：`_resume_execute_after_plan` 悬挂 → 下次普通规划意外弹危险框

成立悬挂的路径（全部实测）：

| 路径 | 触发方式 | 实测 |
|---|---|---|
| A2 feasibility 不可行 | apply → `_generate_plan` 命中 `if self._feasibility ... not ok: return` | flag=True 悬挂 ✓ |
| A3 目标区间非法 | pending 把区间改空 → apply → `end <= start: return` | flag=True 悬挂 ✓ |
| A4 未勾选文件 | apply → `not selected: return` | flag=True 悬挂 ✓ |
| A5 规划返回非 MovePlan | `_on_plan_ready` 的 `not isinstance(plan, MovePlan): return` 不清标志 | flag=True 悬挂 ✓ |
| A1 卷/位图未就绪 | `_start_move` 在 apply 分支**之前**就 return | flag=False，**不可达，无风险** |

**后果实测（B1）**：悬挂后用户把条件改好、只是正常"生成计划"，`_on_plan_ready` 末尾的 resume 块被触发 → **DangerConfirmDialog 意外实例化 1 次**（用户没请求执行却弹执行确认框）。因确认框仍是人工闸门，不会无确认写盘，故定 P2 而非 P1；但"点生成计划却弹出执行确认"违背最小意外。

**修法建议**：把 resume 意图绑定到**本次规划**而不是窗口全局状态 ——
`_generate_plan` 里 `worker.resume_after = self._resume_execute_after_plan; self._resume_execute_after_plan = False`，`_on_plan_ready/_on_plan_failed` 从 `self.sender()`（或传入的 worker）读取该属性。这样任何 early-return 都不可能留下悬挂（标志只活在 worker 上）。

### 9.3 缺陷 2（P1，既存但被本修复放大）：规划中按 Esc → UI 永久 busy

`PlanWorker.run` 在 `self._cancel` 为真时 **`return`**，既不 emit `completed` 也不 emit `failed`；而 `_plan_worker` **只连了 completed/failed，没有 finished 兜底**（`main_window.py` 中 `worker.finished` 只给 FeasibilityWorker 接过）。实测 F1：`request_cancel()` 后 `start()`+`wait()`，两个信号 **events=[]**。

后果：`_set_busy(False)` 永不执行、`_plan_worker` 永不置 None → 进度条一直 indeterminate、按钮全禁用，用户只能关窗口；同时 `_resume_execute_after_plan` 保持 True（下次任何规划成功仍会后置误弹框）。

这是 800f4a4 之前就存在的缺陷，但本修复把"apply → 自动开始规划"变成高频路径：用户点了「先应用再执行」看到"正在重新生成计划…"后按 Esc 想反悔，就会撞上。

**修法建议**：`_plan_worker.finished.connect(self._on_plan_worker_finished)`，在槽里统一 `_set_busy(False)` + `_plan_worker=None` + 清 resume 标志（若本次规划没有产出结果）。同类问题也存在于 ScanWorker/IndexWorker 的取消路径，建议一并核。

### 9.4 其他确认（无问题）

- 接续路径仍完整走 admin 检查、plan 非空、跨卷拦截、DangerConfirmDialog —— 未跳过任何人工闸门。
- 两处静默 return 补提示（卷/位图未就绪、缺计划）文案准确（钉子⑥）。
- `proceed` 分支不置标志（钉子④）；`_on_plan_failed` 清标志（钉子⑤ + 代码）。
- 切分块流式引擎：规划期间 `io_panel.set_busy(True)` 禁用，切不了；**但**若已悬挂且用户随后切到 stream 引擎，`_on_plan_ready` 的 resume 会调 `_start_move()`（LBA 路径）而不是流式路径——属悬挂的次生后果，修好 9.2 即消失。
- 连续两次 apply：`_has_pending` 已 False，第二次点执行走正常 `_start_move`，不重复置标志，无重复执行风险。

### 9.5 放行判断

**有条件通过**：9.2（P2，标志悬挂 + 意外弹框）建议本次修掉（改动小）；9.3（P1，Esc 卡死）建议同批修 `finished` 兜底——它虽是既存缺陷，但会让"先应用再执行"这条刚修好的链路变成死路。两者修完我做 5 分钟定向复验（`d5` 全绿 + testflow）。

---

## 10. d2be150 复核（9.2 悬挂 / 9.3 取消卡死 两条修复）

取证：`qa_work/d5_resume_flag_check.py`（**19 项断言全过**，`d5_output.txt`）、`tests/test_ui_execute_flow.py`（**13/13**）、testflow **322 passed / 0 failed**（独占跑，40.2s）。

### 10.1 修法审查（读 diff）

**① 意图悬挂（P2）→ 改为「意图随本次规划生灭」**
- `_generate_plan` **入口第一件事**摘意图（`resume_after = self._resume_execute_after_plan; self._resume_execute_after_plan = False`），随后挂到 `worker.resume_after` → 4 处 early-return 都不可能在窗口上留下悬挂。
- `_take_resume_intent(worker=None)`：优先读 `sender()`（或显式 worker），**消费即清**（worker 与窗口标志都清），回落窗口标志覆盖"刚被 apply 置上、还没交给 worker"的瞬时值。
- `_on_plan_ready` **首行**即消费意图（在 `not isinstance(plan, MovePlan)` 那处 return 之前）——这正是 9.2 里我列的第 4 条路径。
- early-return 统一走 `_blocked()`：若本次原本要自动接续，提示追加「（配置已应用，自动执行已取消，请调整后手动点击开始迁移）」+ warn 日志 → 不再"静默取消"。

**② 取消卡死（P1）→ finished 兜底**
- `worker.finished.connect(self._on_plan_worker_finished)`。
- 兜底槽有两道 no-op 守卫：`worker is not self._plan_worker → return`（旧任务已被替换）、`pending is None → return`（completed/failed 已收尾）。只在"线程结束但无终态信号"时接管：清 `_plan_worker`、作废意图（含 worker 上的）、reset 进度、退忙碌、notice、`_sync_buttons()`。
- `scanner.py` 把"取消后 completed/failed 都不发、调用方必须用 finished 兜底"写成契约注释 —— 这个雷点显式化是有价值的（我 9.3 提过同模式的 ScanWorker/IndexWorker，工程师核后确认它们都会 emit 终态，只有 PlanWorker 是裸 return）。

### 10.2 实测（19/19 全过）

| 组 | 断言 | 修复前 | 修复后 |
|---|---|---|---|
| A2 | feasibility 不可行早退 | flag=True（悬挂） | **False** ✓ |
| A3 | 目标区间为空早退 | flag=True | **False** ✓ |
| A4 | 未勾选文件早退 | flag=True | **False** ✓ |
| A5 | `_on_plan_ready` 收到非 MovePlan | flag=True | **False** ✓ |
| A1 | 卷/位图未就绪 | 不可达（无风险） | 同 |
| B1 | 悬挂后普通规划完成 | DangerConfirmDialog **1 次**（意外） | **0 次** ✓ |
| C1–C3 | 正常接续 | busy=False / plan_worker=None / 确认框 1 次 | 同（未回归）✓ |
| D1 | 连续三次 `_on_plan_ready` | 只接续 1 次 | 同 ✓ |
| E1–E3 | 反向验证旧写法 | 钉子①⑥变红、②绿 | 同 ✓ |
| F1 | worker 层取消不发终态信号 | events=[] | 同（契约，UI 侧兜底）✓ |
| G1–G5 | **UI 层取消不卡死**（真实 PlanWorker：start→cancel→wait→processEvents） | 永久 busy | `_plan_worker=None`、`_busy=False`、意图两处均 False、notice="规划已取消，可调整参数后重新生成计划" ✓ |

### 10.3 测试稳定性问题（P3，非产品缺陷）

`tests/test_blockers_perf.py::test_find_blockers_scale_is_sublinear` 的 600ms 预算是**紧贴实测值**的：我单跑三次为 **501.2 / 584.1 / 506.1 ms**（余量 3%~17%）。我在并发跑其他验证时它确实红过一次（testflow 报 `tests-failed · 1 failed / 321 passed`）；独占重跑即 PASS。建议把预算提到 **1200ms**，或把基准规模减半并改相对比较——否则 CI 会持续偶发红，且难以与真实性能回归区分。

另：本轮第一次 testflow 报 `INVALID (source-changed)`，原因是验证期间仓库仍有写动作（06:28–06:29 的文件 mtime）；随后 `git status` 干净（`tests/test_ui_execute_flow.py` 与 `CHANGELOG.md` 的 `M` 经 `git diff HEAD` 确认为空 diff，仅行尾/stat 抖动），独占重跑得到 322/0。

### 10.4 放行结论

**d2be150 通过。** 9.2（悬挂 + 意外弹框）与 9.3（取消卡死）两条均已闭环，且都有实测与钉子守住；接续仍走 DangerConfirmDialog、无二次执行、无回归。

**「先应用再执行」链路至此完整可用。1.0.0D 系列（12 个本地 commit）QA 侧无未决阻断项**，仅剩 10.3 的测试预算建议（P3）。
