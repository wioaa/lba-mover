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

---

## 11. 定向复验 d2be150 + 856704a（主理人指定的 5 项）

取证：`qa_work/d5_resume_flag_check.py`（19 项）、**`qa_work/d6_nail_falsify.py`（16 项，新增加强证伪）**、`tests/test_ui_execute_flow.py`（13/13）、testflow **322 passed / 0 failed**（44.0s）。HEAD = `856704a`。

### 11.1 ② 悬挂残留证伪 —— 全部无残留

| 情形 | 结果 |
|---|---|
| `_on_plan_ready` 收到**非 MovePlan** 早退 → 再收到有效计划 | 不误接续（`moves=[]`）✓ |
| **连续两次 apply**（均撞非法区间） | 两次后 `flag` 均 False，不累积 ✓ |
| apply → 改回不合法区间 → 点「生成计划」 | DangerConfirmDialog **0 次** ✓ |
| 反面对照：真的带意图（**真 Qt 信号** emit） | 恰弹 **1 次**，且 worker/窗口意图双清 ✓ |

### 11.2 ③ `_on_plan_worker_finished` 幂等性 —— 正确

| 情形 | 结果 |
|---|---|
| 正常收尾（`_plan_worker is None`）后调用 | **no-op**，`_busy` 保持 True（不误清别的任务）✓ |
| **旧任务**（已被替换）发 finished | 不误清新槽位、不误退忙碌态 ✓（必须用真信号 emit，直接调槽 `sender()` 为 None 会绕过该守卫——这是测试写法问题，非缺陷） |
| 两次规划：第 1 次取消 → 第 2 次正常 | 取消后 `busy=False/plan_worker=None/意图作废`；第 2 次完成后同样回到干净态 ✓ |

### 11.3 ④ 钉子反证 —— 有效

- **摘掉 `finished` 连接**（把 `_on_plan_worker_finished` 换 no-op）→ UI 层取消后**确实卡死**（`busy=True`、`_plan_worker` 非空）→ 证明钉⑩ 能抓住该回归。
- **把 `_take_resume_intent` 改回"只读窗口标志"**（忽略 `worker.resume_after`）→ 带意图的规划完成后**不再接续**（`moves=[]`）→ 证明行为确实依赖 worker 上的意图，钉⑧ 守住的是这条路径。

### 11.4 ⑤ 核对「其余 worker 取消都会发终态信号」

**结论：成立，但需精确一处。** 逐类读 `scanner.py`：

| Worker | 取消后的出口 | 是否会卡死 |
|---|---|---|
| BitmapWorker | `failed.emit("位图读取已取消")` | 否 |
| ScanWorker | `_run_handle_scan` 两处 `completed.emit(results)`；MFT 路径取消 → 回退句柄 → 最终完成 | 否 |
| IndexWorker | `ready` 或 `failed` 必发一个 | 否 |
| MftScanWorker | `completed.emit(entries)` | 否（但用户主动取消可能显示成"通道已回退"，P3 文案歧义，工程师已承认未动） |
| MoveWorker / RollbackWorker / BlockStreamWorker | `completed.emit(outcome)` | 否 |
| **FeasibilityWorker** | **也是裸 return**（`if self._cancel: return` ×3） | 否——UI 早已连 `finished`（`_on_feasibility_finished`）且它不参与 `_busy` |
| **PlanWorker** | 裸 return | 已由 `_on_plan_worker_finished` 兜底修复 |

所以精确表述是「**只有 PlanWorker 需要新增 finished 兜底**」成立；「仅 PlanWorker 是裸 return」严格说不成立（FeasibilityWorker 同为裸 return，只是已有兜底）。

### 11.5 新发现（P3，不阻塞）

`_on_plan_worker_finished` 在 **`sender() is None`**（被直接调用而非信号触发）时会走"接管收尾"分支，把当前槽位清掉。真实 Qt 路径不可达（`finished` 必由信号触发），但若将来有人直接调它做手动收尾就会误清。建议加一行 `if worker is None: return`。

### 11.6 放行结论

**放行通过。** d2be150 的两条修复（意图绑 worker、finished 兜底）经加强证伪后均无残留，13 例钉子有效（变异后确实变红），testflow 322/0。工程侧另把性能预算从 600ms 调到 1200ms（`tests/test_blockers_perf.py`，工作区改动未提交）——采纳了我的建议，方向正确。

**1.0.0D 系列 QA 侧无未决阻断项；「先应用再执行」链路完整可用。** 仅余 3 条 P3：性能预算改动待提交、`MftScanWorker` 取消文案歧义、`_on_plan_worker_finished` 对 `sender() is None` 的防御性。

---

## 12. 追加：取消语义复核（原 P3 升级为 P2）+ 性能预算定稿意见

HEAD = `3c9692d`（工作区干净，除本报告）。

### 12.1 性能预算 1200ms：认同定稿

工程师实测独占跑三次 529.8 / 433.7 / 606.9 ms（旧 600ms 预算下 1/3 概率假红），我复现了量级。另按同一场景构造做「同 run 内三档」测量（`qa_work/d7_ratio_probe.py`，每档 3 次取最优）：

```
25,000 extent → 12,500 blockers， 82.2 ms
50,000 extent → 25,000 blockers，193.2 ms
100,000 extent → 50,000 blockers，391.1 ms
   100k / 25k = 4.76× ；100k / 50k = 2.02×
```

**结论：1200ms 定稿可接受。** 依据：观测上限 606.9ms → 约 2× 余量；真回归的对拍基线 11,532 ms 高一个数量级，必红。补充数据说明「改相对比较」收益有限——4× 规模只带来 4.76× 耗时，说明当前耗时主体是 O(E) 的 Blocker 构造而非 E×R 裁剪，比值断言的余量同样约 2×，仅换来"免疫机器速度"，不构成必须项。

### 12.2 扫描/索引取消语义：P3 → **P2**（三处落点）

工程师原描述「取消 → `scan_volume_mft` 返回非 ok → `_emit_fallback` → UI 显示通道已回退」**与代码不符**，实测（`qa_work/d8_mft_cancel_probe.py`）证明：

1. 取消分支只 `outcome.notes.append("扫描已取消（结果不完整）"); break`（`mft_reader.py:502-504`），**不设 `fallback_code`**；
2. `MftScanOutcome.ok = not fallback_code`（`mft_reader.py:72-73`）→ 取消后 **`ok is True`**；
3. `MftScanWorker.run` 的 `if not outcome.ok: _emit_fallback(...)` 不成立 → 走**成功**分支；
4. 桩测：`completed=1`、`fallback=[]`、`failed=[]`；
5. `run()` 从未把 `outcome.notes` 传给任何信号 → 那句"扫描已取消（结果不完整）"**永远不会显示**。

**实际后果**：用户按取消，UI 得到「扫描完成」+ 一份**不完整的文件列表**，无任何提示。这不是文案问题而是语义丢失：用户可能据此认为某些文件不在该卷上，进而做出错误的迁移选择（无数据破坏，但结论错误）。

**补充第三处落点**（工程师与主理人逐行核对后确认）：`_on_index_ready` —— `IndexWorker` 取消后仍 `ready.emit(payload)` 且 `ok` 可能为 True，UI 会打印「归属索引就绪」，把**部分归属表**当完整索引使用；此外 `_on_scan_completed` 取消后仍**无条件 `_build_index()`**，会拿部分数据建一次全卷索引（白跑）。

**建议修法**：`MftScanOutcome` 增 `cancelled` 标记（或专用 `FALLBACK_CANCELLED`）；`MftScanWorker.run` 在 cancelled 时照常 emit 已解析的 `found`，但完成态文案改为「扫描已取消，仅显示已解析的 N 项」。**同一问题也存在于 `ScanWorker._run_handle_scan`**（两处 `completed.emit(results)`），建议一并处理。

### 12.3 建议

P2 推迟到 1.0.0E 修复（不阻塞当前放行：不卡死、不误启动写盘、不影响已有数据的正确性），但应记入 1.0.0E 待办，并且**在"扫描"这条链路上加一枚钉子**：取消后 UI 文案必须含「已取消」。

---

## 13. 1.0.0D 发布核对 + 1.0.0E 验收判据

### 13.1 发布核对（可验证项）

| 项 | 结果 |
|---|---|
| 代码基线 | 本地 `HEAD` = `3c9692d`（06:33:51），工作区干净（除本报告） |
| 打包产物 | `dist/LBA-Tail-Mover.exe`（47,044,920 B，构建于 06:35:29，晚于最后一次源码改动 06:29） |
| 版本资源 | `ProductVersion = 1.0.0D`、`FileVersion = 1.0.0.0`、`ProductName = LBA Tail Mover` ✓ |
| 远端 main | **已证实 = `3c9692d`**（主理人侧 `GET /repos/wioaa/lba-mover/git/ref/heads/main` 返回 `3c9692db7…`，最近 3 个 commit 逐条与本地一致）。本机早前看到 `origin/main=40d7d28 / ahead 16` 系 **local tracking 残留**（推送经 Git Data API 完成，不会自动更新 tracking ref）；`git update-ref` 对齐后本机 `git branch -vv` 显示 `main 3c9692d [origin/main]`、无 ahead（QA 已复核） |

推送方式说明（记录）：本机到 `github.com` 的 git 主机不可达（`Connection reset`），本次及此前数次推送均走 **Git Data API 兜底**（blobs/tree/commits + PATCH refs），重建 SHA 与本地逐字一致，故远端内容与本地等价。

### 13.2 1.0.0E 验收判据（扫描取消语义，待实现后逐条验证）

对 `MftScanWorker` 与 `ScanWorker._run_handle_scan` 各一组：

1. **文案**：取消后状态/提示必须含「已取消」，且**不得**出现「完成」字样（两者互斥）。
2. **计数一致**：提示里的「已解析 N 项」必须等于本次 `completed` 携带的条目数（不虚报、不少报）。
3. **`fallback_code` 不被污染**：取消后 `fallback_code` 为空或专用取消码；**不得**等于 `not_ntfs` / `no_privilege` / `mft_runlist_unavailable` / `structure_invalid` / `bounds_violation` / `sample_mismatch` 等真实降级码（否则日志与统计会把"用户取消"记成"通道失效"）。
4. **不被当作降级**：取消后 `channel` 应保持 `"mft"`（MFT 路径）/ 保持句柄通道原语义，**不得**因取消而切成 `"handle"`。
5. **部分结果可用**：取消前已 `found` 的条目仍应出现在表格里（不丢已解析结果）；表格内容 = 已解析集合，不得混入未解析的占位。
6. **不误设 `fatal_error`**：取消后 `stats.fatal_error` 应为空（`fatal_error` 只用于真实失败）。
7. **回归钉**：至少一枚断言「取消 → 文案含已取消 + `fallback_code` 未被污染 + 计数与条目数一致」，防止再次退化。
8. **若同批修 P3-1**（`_on_plan_worker_finished` 的 `sender() is None` 守卫）：`test_plan_cancel_fallback_clears_busy` 与 `test_plan_finished_is_noop_after_terminal_signal` 必须改为**真 Qt 信号驱动**，并新增一枚「直调该槽为 no-op」的契约钉子。

验证脚本计划：`qa_work/e1_scan_cancel_check.py`（桩 `scan_volume_mft` 返回取消态 outcome + 桩 `ScanWorker` 的扫描结果，逐条断言上述 1–6），配合 testflow 全量。

---

## 14. 1.0.0E 在途实现验收（工作区快照，非提交版）

> 状态说明：本节结果针对**未提交的工作区**（`app/core/scanner.py`、`app/ui/main_window.py`、`app/win32/mft_reader.py`、`tests/test_ui_execute_flow.py` 均为 M）。提交后需重跑。

脚本：`qa_work/e1_scan_cancel_check.py`（11 项断言），结果 **10 绿 / 1 红**。

### 14.1 已达标

**MftScanWorker（8/8）**：`progress = "扫描已取消，仅显示已解析的 1 项"`；`completed` 条目数 1；`fallback=[]`；`fallback_code=''`；`channel='mft'`；`found=1`；`fatal_error=''`。
即 §13.2 的 1/2/3/3b/4/5/6 全部满足。

**ScanWorker + UI（2/3）**：`cancelled` 只读属性存在、`request_cancel()` 后为 True；真信号驱动下 notice =「扫描已取消：仅显示已解析的 1 项，可重新扫描获取完整列表」，且不含「扫描完成」。

### 14.2 新发现（P3）：`_on_scan_completed` 的 `sender()` 直调陷阱

```python
cancelled = bool(getattr(self.sender(), "cancelled", False))
```
直调该槽（测试/复用/换信号源）时 `sender()` 为 None → **取消被静默当成「完成」**，实测 `notice=''`。生产路径是信号驱动故当前无碍，但会让"直调式测试"假绿（只断言表格条数就过，取消文案从未被验证）。

**建议**：追加 `or bool(getattr(self._scan_worker, "cancelled", False))` 兜底（`_scan_worker` 在该行之后才置 None，可读）。采纳后 e1 第 10 条应由红转绿，列为 1.0.0E 必过项。

> 这是本项目第 4 次踩到"依赖 `sender()` 的槽被直调/替身驱动"的坑（工程师 1 次、QA 2 次、本次 1 次）。建议在回归方法论里固定一条：**凡读 `sender()` 的槽，测试必须用真 Qt 信号驱动，或实现侧提供非 `sender()` 兜底**。

---

## 15. 基线校准（本节为最终口径）

| 项 | 最终状态 |
|---|---|
| 1.0.0D 代码基线 | `3c9692d`；**远端 main = `3c9692d` 已证实**（GitHub API `git/ref/heads/main`）；本机 `git branch -vv` = `main 3c9692d [origin/main]`、无 ahead（local tracking 残留已由 `update-ref` 对齐） |
| 打包产物 | `dist/LBA-Tail-Mover.exe` 47,044,920 B，`ProductVersion=1.0.0D` |
| testflow | 322 passed / 0 failed（`3c9692d`） |
| §11 的"性能预算未提交" | **作废**，以 §12 为准（1200ms 已入库） |
| §12.2 的 P2 范围 | 三处落点：`_on_scan_completed`（取消仍报完成 + 无条件 `_build_index()`）、`_on_index_ready`（取消仍报"就绪"、部分归属表当完整用）、三个 Worker 缺 `cancelled` 标识 |
| 1.0.0E 第一批 | ①P3-1 `worker is None` 守卫 + 两枚钉子改真信号；②P2 扫描/索引取消语义（上述三处）+ `ScanWorker._run_handle_scan` 两处同源；③"只有 PlanWorker 是裸 return"文案订正；④版本 1.0.0E 三处同步 |

### 15.1 e1 验收脚本现状（针对**未提交**工作区快照）

`qa_work/e1_scan_cancel_check.py`：**16 项断言，14 绿 / 2 红**。

- ① MftScanWorker 取消语义 **8/8 绿**（文案、N 一致、`fallback_code` 未污染、channel 未切、部分结果不丢、`fatal_error` 空）；
- ② ScanWorker：`cancelled` 属性 ✓、真信号路径 UI 文案 ✓；
- ③ IndexWorker/UI：取消不写 `_ownership` ✓、调用 `set_blockers_unavailable` ✓、退忙碌态 ✓、非取消路径未误杀 ✓；
- **2 红同源**：`_on_scan_completed`（第 10 条）与 `_on_index_ready`（第 13 条）在**直调**（`sender() is None`）时取消被当成"完成/就绪"。修法：两处各加 `or bool(getattr(self._<x>_worker, "cancelled", False))` 兜底。**这两条为 1.0.0E 必过项。**

> 已请工程师自查本批**所有** `sender()` 读取点（含 `_on_plan_ready` / `_on_plan_failed` / `_on_plan_worker_finished` / `_on_feasibility_ready` / `_on_scan_completed` / `_on_index_ready`），凡无 `_xxx_worker` 兜底者一并补。

---

## 16. 1.0.0E 第一批 + 第二批 正式验收（基线 `f1b4a80`）

取证：`qa_work/e1_scan_cancel_check.py`（**18 项断言全绿**）、`qa_work/d9_e1_extra_falsify.py`、testflow、离屏冒烟。工作区仅 `tests/test_cancel_semantics.py` 有未提交改动（+41 行，调用点钉子，见 16.4）。

### 16.1 上轮两个红点：已转绿

| 断言 | 结果 |
|---|---|
| ⑩ `_on_scan_completed` **直调** → notice 含「已取消」 | PASS（`扫描已取消：仅显示已解析的 1 项，可重新扫描获取完整列表`） |
| ⑬ `_on_index_ready` **直调** → `_ownership is None` | PASS |
| ⑬ 隐含验证「兜底读取必须在 `_index_worker = None` 之前」 | PASS —— 直调时 `sender()` 为 None，若顺序颠倒则读到的 `cancelled` 必为 False，⑬ 必红。故 ⑬ 同时是**顺序契约钉子** |

### 16.2 新增实现细节证伪

| 项 | 结果 |
|---|---|
| `_on_scan_cancelled` 自行 `_set_busy(False)` | PASS（9c：取消后 `_busy is False`）——取消不建索引，不自己退会重演 P1 卡死 |
| 非取消路径**不得**误调 `_on_scan_cancelled` | PASS（9d：spy 为空） |
| 索引取消走 `set_blockers_unavailable` 而非静默 | PASS（11b：调用 1 次；11：`_ownership is None`；11c：退忙碌 + 槽位清空） |
| 非取消索引路径未误杀 | PASS（12：`_ownership` 正常写入） |
| `cancelled` 与 `ok` 语义不冲突 | PASS（③ `fallback=[]`、③b `fallback_code=''`、④ `channel='mft'`、⑥ `fatal_error=''`，即取消时 `ok` 仍 True、不污染降级码） |

### 16.3 反向验证（我独立执行，非引用其自述）

| 变异 | 结果 |
|---|---|
| `mft_reader.mark_cancelled` 置 no-op | `test_cancel_semantics.py` → **2 枚钉子变红**（`test_mft_reader_marks_cancel_on_outcome`、`test_mft_scan_worker_cancel_reports_completed_with_flag`）→ 证实「原先无钉子覆盖取消置位」的说法成立 |
| 把 `query` 换成 O(N) 全表扫（复杂度退化模拟） | 比值 **0.94 → 6.17**，越过阈值 4.0 → ownership 的比值断言**确有区分力**（现状 0.94~1.81，阈值 4.0，退化 6.17；线性真退化会更接近 10） |
| 调用点钉子（工作区新增）：把 `scan_volume_mft` 换成「取消分支只 break 不 mark」 | 钉子**变红**；还原后**通过** → 该钉子真实锚定「调用点是否接线」 |

### 16.4 `sender()` 六处清点表：三处「不加兜底」的判断我全部认同

| 槽 | 判定 | 我的意见 |
|---|---|---|
| `_on_scan_completed` | 加 `_scan_worker` 兜底 | 对（直调会把取消当完成） |
| `_on_index_ready` | 加 `_index_worker` 兜底，且**在置 None 之前** | 对（顺序错了就失效，⑬ 可证） |
| `_on_plan_worker_finished` | `is None → return`（不兜底） | **对**。兜底会清掉"可能仍在跑"的槽位 → 双重规划线程，比不兜底危险 |
| `_take_resume_intent` | 只认 sender + 窗口瞬时标志 | **对**。误判方向不对称：误判 → 无端弹执行确认框（危险）；漏判 → 少一次自动接续（安全）。且 `_plan_worker` 可能已是新线程，读它会把上一轮意图错配给新一轮 |
| `_on_feasibility_ready/_failed` | 用 `seq` 代次 | **对**。代次天然区分新旧，比身份比对更可靠（且能处理"同一 worker 重投"场景） |

判定原则可归纳为一条，建议写进方法论：**兜底只在"兜底对象代表同一逻辑任务"时成立；否则按误判方向的危害选择失败方向**。

### 16.5 收尾数据

| 项 | 结果 |
|---|---|
| testflow（工作区，含调用点钉子） | **335 passed / 0 failed**（45.2s） |
| testflow（`f1b4a80` 提交版） | 334 passed / 0 failed |
| 版本 | `display_version() == "1.0.0E"` |
| 写路径隔离 | `MOVER_CLEAN`；初始 `_plan_worker=None / _busy=False` |

### 16.6 放行结论

**放行，可打包推送。** 生产代码侧（`fadad65` + `f1b4a80`）已达标：取消不再被报成完成、不自动勾选、不建残索引、不卡忙碌态、`fallback_code` 与 `ok` 语义未被污染；两处直调兜底与顺序契约均生效。

**唯一待办**：第三个 commit（仅测试，+41 行调用点钉子）仍未提交——它不改变运行时行为，所以不阻塞打包；但**必须提交**，否则"取消分支被删掉调用行"这一层永远没有钉子看守（我已实测该钉子在变异下会红、还原后通过）。

> **本章条已关闭**：该批已作为 `3d2aa27` 提交（`tests/test_cancel_semantics.py` +41、`CHANGELOG.md` +1），随后 `dea5d0f` 把本报告 §13~§16 入库并推到远端。见 §17。

---

## 17. 1.0.0E 正式验收（基线 `3d2aa27`，含第三批调用点钉子）

**最终发布基线 = `dea5d0f`（远端 `main`，我用 `git ls-remote origin refs/heads/main` 独立核过）**。`dea5d0f` 相对 `3d2aa27` 只有一处改动：`docs/QA-REPORT-1.0.0D-commits.md` +232 行（本报告 §13~§16），**零源码、零测试**。因此本章对 `3d2aa27` 的全部实测结论对 `dea5d0f` 逐条成立，不需要重跑；team-lead 侧在 `dea5d0f` 上另跑过 testflow = 335/0，与本机结果一致。

取证脚本：`qa_work/e1_scan_cancel_check.py`、**`qa_work/e2_callpoint_nail_audit.py`**（新增，6 变异判别力审计）、**`qa_work/e2b_proposed_nail_check.py`**（新增，验证我建议的加固写法）、`qa_work/e3_offscreen_smoke.py`。工作区 `git status` 干净（`dea5d0f` 已把本报告入库），源码文件 sha256 `b99186c4…`，变异后逐次还原并校验一致。

### 17.1 三项要求：全部达成

| 项 | 结果 |
|---|---|
| `e1`（对 `3d2aa27` 提交版） | **18/18 PASS**，脚本自判"全绿：取消语义已达标" |
| testflow 全量 | **335 passed / 0 failed / 0 errors / 0 skipped**（44.7s，`reason=ok`） |
| 用例数核对 | `test_cancel_semantics.py` **12**、`test_ui_execute_flow.py` **14**，全仓 `def test_` 合计 **335**，与 testflow 汇总一致 |
| 版本 / 隔离 / 冒烟 | `display_version()=="1.0.0E"`；`app.win32.mover` 未加载；主窗可构建、初始 `_busy=False`、`_plan_worker=None` |

### 17.2 第三批调用点钉子：变异审计（我独立做，非引用其自述）

钉的是 `test_scan_volume_mft_cancel_branch_calls_mark_cancelled`（AST 扫 `scan_volume_mft`，要求"含 `should_cancel` 的 `if` 分支 body 同时含 `mark_cancelled(...)` 与 `break`，且此类分支恰好 1 处"）。

| 变异（我施加在真源文件上，跑完逐次还原） | 行为是否坏了 | 调用点钉子 | 函数钉子 | 期望 | 判定 |
|---|---|---|---|---|---|
| 基线 `3d2aa27` | — | **绿** | **绿** | 绿 | OK |
| M1 删 `mark_cancelled` 留 `break`（工程师复现过的洞） | 坏 | **红** | 绿 | 红 | **OK，且证明它钉的是接线不是实现** |
| M2 删 `break` 留调用 | 坏（取消后仍扫完整个 MFT） | **红** | 绿 | 红 | OK |
| M3 `break` 前置（调用变死代码） | 坏 | **绿** | 绿 | 红 | **假绿（漏网）** |
| M4 调用包进 `if False:` | 坏（永不置位） | **绿** | 绿 | 红 | **假绿（漏网）** |
| M5 守卫改本地别名 `probe = should_cancel`（纯重构） | **不变** | **红** | 绿 | 绿 | 假红（过硬，但红是安全方向） |
| M6 在路径重建阶段**新增第二个正确取消分支** | **更好** | **红** | 绿 | 绿 | **假红（过硬，正常演进会误伤）** |

- 结论 A（回答"变异是否真红"）：**是**。M1 下只有调用点钉子红、函数钉子仍绿，与工程师自述一致——它确实锚定"接线"这一层。
- 结论 B（回答"恰好 1 处会不会误红"）：**会**。`len(hits) == 1` 把"分支数量"也钉死了：将来在采样/路径重建阶段加第二个**完全正确**的取消分支（M6），语义更强却判红。建议改成"**每一处**取消分支都必须接线"，而不是"**只有一处**取消分支"。
- 结论 C（新发现的漏网方向）：现状断言只要求"同一 body 里既有调用又有 `break`"，**不检查顺序、不检查可达性**。所以 `break; mark_cancelled(...)`（M3，最像手滑挪行的事故）与 `if False: mark_cancelled(...)`（M4）都是绿的——两者都会让取消语义完全丢失，而函数钉子也照旧绿。**这是比"删掉调用行"更隐蔽的复发路径。**

### 17.3 我建议的加固写法（已在内存中验证判别力，未改仓库）

```python
branches = [n for n in ast.walk(tree) if isinstance(n, ast.If)
            and any(isinstance(s, ast.Name) and s.id == "should_cancel" for s in ast.walk(n.test))]
assert branches, "找不到任何以 should_cancel 为守卫的取消分支（取消能力被删？）"
for node in branches:
    call_idx = next((i for i, st in enumerate(node.body)
                     if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call)
                     and getattr(st.value.func, "id", "") == "mark_cancelled"), None)
    break_idx = next((i for i, st in enumerate(node.body) if isinstance(st, ast.Break)), None)
    assert call_idx is not None, "取消分支没有直接调用 mark_cancelled(...)"
    assert break_idx is not None, "取消分支没有 break"
    assert call_idx < break_idx, "mark_cancelled 在 break 之后 → 死代码"
```

`qa_work/e2b_proposed_nail_check.py` 实测（纯文本变异，不写盘）：

| | M1 | M2 | M3 | M4 | M5 | M6 |
|---|---|---|---|---|---|---|
| 现状写法 | 红 | 红 | **绿** | **绿** | 红 | **红** |
| 建议写法 | 红 | 红 | **红** | **红** | 红 | **绿** |

即：3 个问题变异修好 2 个（M3/M4），M6 的误红消除；**M5（把守卫改成别名）仍会红**——任何靠 AST 认名字的钉子都做不到跟随别名，这一条只能靠"红了就去改测试"消化，方向安全（响而不是哑）。

### 17.4 打包状态

`dist/LBA-Tail-Mover.exe` 资源 `ProductVersion=1.0.0E`、`FileVersion=1.0.0.0`，时间戳 `2026-10-24 06:52:59`，**晚于 `3d2aa27`（06:51:33）**，之后只有文档 commit `dea5d0f`。即现有产物已含全部 1.0.0E 运行时改动（第三批仅测试，不影响产物）。重打包可做但非必需。注：PyInstaller 压缩了 pyc，字节里搜不到 `mark_cancelled` 等符号属正常，不能作为"未打进去"的证据。

**`tests/` 不进产物（已核 `.spec`）**：`datas=[]`、`scripts=[main.py]`，`Analysis` 从入口沿 import 图收集；`app/` 从不 import `tests/`，故任何**纯测试 commit 都不会改变 exe 字节**，也不需要重打包。这条是"P3 钉子硬化可以在 1.0.0E 发布后再补一个测试 commit"的依据。

### 17.5 放行结论

**放行，可打包推送三个 commit。** 阻断/严重级 0 条。

新增 1 条 **建议级（P3，不阻塞）**：调用点钉子的判别边界——`hits == 1` 过硬（M6 误红）、且不辨顺序/可达性（M3/M4 漏网）。修法见 17.3，可在 1.0.0E 之后单独一个测试 commit 处理；**不影响本次发布**，因为取消语义本身已由 5 层钉子把守（函数钉子 + 调用点钉子 + worker `cancelled` 属性 + UI 取消分支 + 直调兜底），漏掉的只是"将来有人挪行"的一种形态。

### 17.6 发布状态（收尾）

| 项 | 状态 |
|---|---|
| 远端 `main` | `dea5d0f`（`git ls-remote` 独立核对一致），本地 `main` 无 ahead/behind |
| 推送链 | `fadad65` → `f1b4a80` → `3d2aa27` → `dea5d0f` |
| 产物 | `dist/LBA-Tail-Mover.exe` `ProductVersion=1.0.0E`；`--selftest` exit=0（team-lead 侧） |
| §16.6「唯一待办」 | **已关闭**（由 `3d2aa27` 交付） |
| 本报告 §17 | 本次新增，尚未入库（唯一未提交文件） |
| P3 钉子硬化 | 已转工程师，建议在下一批次单独一个测试 commit 处理 |

---

## 18. P3 闭环：调用点钉子硬化（`1990122` + `1d1d01c`，本地 ahead 2，待推）

### 18.1 两个 commit 的性质（我先核范围，再跑测试）

| commit | 内容 | `app/` 改动 |
|---|---|---|
| `1990122` | 调用点钉子重写为「接线 + 顺序」，去掉「恰好一处」 | **空** |
| `1d1d01c` | 只加 13 行注释（「有意红」清单 + 覆盖边界） | **空** |

`git diff --stat HEAD~1 HEAD -- app/` 为空；`1d1d01c` 相对 `1990122` 去掉注释行后 diff 为**零**（纯注释）。故两版对**产品代码零影响**，`dist/` 无需重打（依据见 §17.4：`tests/` 不进依赖图）。

### 18.2 六格变异矩阵：`3d2aa27` → `1d1d01c` 的实测变化

`qa_work/e2_callpoint_nail_audit.py`（写进真源 → 跑 → 还原 → 校验 sha256，本机实测）：

| 变异 | 行为 | `3d2aa27`（旧钉子） | `1d1d01c`（新钉子） | 期望 |
|---|---|---|---|---|
| M1 删调用留 `break` | 坏 | 红 | **红** | 红 |
| M2 删 `break` 留调用 | 坏 | 红 | **红** | 红 |
| M3 `break` 前置（死代码） | 坏 | **绿（假绿）** | **红** ✅ | 红 |
| M4 调用包 `if False:` | 坏 | **绿（假绿）** | **红** ✅ | 红 |
| M5 守卫改别名 | 不变 | 红（过硬） | **红（有意保留）** | 红（已接受） |
| M6 新增第二个正确分支 | 更好 | **红（假红）** | **绿** ✅ | 绿 |

脚本自判：**「无假绿、无假红：钉子的判别力边界与设计意图一致。」** 两个假绿 + 一个假红全部消除，M5 由「意外过硬」转为「文档化的有意红」。

### 18.3 新钉子的实现要点（我逐条对照过，与我在 §17.3 验证的算法等价）

`_cancel_branches`（全部以 `should_cancel` 为守卫的 `ast.If`，不限数量）+ `_first_index`（**只扫直接子语句**、返回下标）+ `_is_mark_cancelled_call`（`ast.Expr` + `Call` + 裸名 `mark_cancelled`）+ 四条断言链（`branches` 非空 → `call_at` 存在 → `break_at` 存在 → `call_at < break_at`）。失败信息带 `lineno`，比我的版本更好定位。三个 helper 以 `_` 前缀命名，`test_*` 收集不受影响（用例数仍 12）。

### 18.4 已知且**有意不覆盖**的边界（已写进 docstring，我也认同）

| 形态 | 新钉子 | 是否为缺陷 |
|---|---|---|
| 守卫改名 / 抽成 `_check_cancel()` 辅助函数 | 红 | 不是——AST 认名字跟不了别名，**红是响不是哑**（M5 同类） |
| 属性调用 `mft_reader.mark_cancelled(...)` | 红 | 同上，改结构须同步改钉子 |
| 改中断方式为 `raise _ScanCancelled` | 红 | 行为等价，属"改结构"；`continue` 形态则属**真红**（不中断解析） |
| 条件写反 `if should_cancel(...) is False:`（M7） | **绿** | **职责边界**：这一层是"接线+顺序"，判条件语义应交给行为钉子；用 AST 判语义是反模式 |

M7 是我在 §17 之后自己构造的第七个形态，明确**不塞进**这枚钉子——把边界写下来，比无限加断言更有价值。

### 18.5 收尾数据

| 项 | 实测 |
|---|---|
| `e2`（`1d1d01c`） | 基线绿；M1~M5 红、M6 绿；`无假绿、无假红` |
| `e2b` | 已改为**历史对照**（`3d2aa27` vs 加固版），现行判据以 `e2` 为准 |
| testflow（`1d1d01c`） | **335 passed / 0 failed / 0 errors / 0 skipped**（46.7s，`reason=ok`） |
| testflow（`1990122`） | 335/0（59.7s） |
| 用例数 | `test_cancel_semantics.py` 12、`test_ui_execute_flow.py` 14 |
| 真源 sha256 | `b99186c4…`（每次变异后均校验还原） |

> 过程插曲：在 `1990122` 上首跑 testflow 得到 `INVALID (source-changed)`，原因是工程师**并发**在改 `tests/test_cancel_semantics.py`（mtime 落在本次运行窗口内，且改完内容与 HEAD 相同）。这是 §17 已定性的设计行为，重跑即 PASS，**不记为缺陷**。
> 但它暴露了变异探针的真实风险：**就地改真源**会与并发工作互相干扰。为此我验证了「影子副本」方案（§18.6）。

### 18.6 变异探针的改进方案（已验证，建议采纳）

`qa_work/e2c_shadow_mutation_probe.py`：把 `app/` + `tests/` 拷到临时目录 → 改**副本** → 在副本里跑测试。实测：

```
影子副本基线：调用点钉子=绿
影子副本 M1：调用点钉子=红（期望红）
真源文件未被修改：是
结论：影子副本方案 可行，且结论与就地变异一致
```

好处：①真仓库全程只读，**与并发工作零冲突**；②进程被杀也不会留下变异源码（我现在靠 sha256 断言兜底，属于事后检测而非事前预防）。代价是多一次几 MB 的 `copytree`（毫秒级）。

### 18.7 结论

**P3 已闭环，验收 PASS。** 调用点钉子从「钉共存 → 钉接线+顺序」完成升级，两个假绿方向（`break` 前置、`if False:` 包裹）与一个假红方向（分支数量）全部消除，边界已文档化。唯一待办是**推送**（本地 ahead 2）与 §17/§18 一并入库；推送后基线变为 `1d1d01c`，运行时产物不变。
