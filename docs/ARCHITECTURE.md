# 架构说明

本文回答三个问题：**东西在哪、谁负责什么、数据怎么流**。取舍理由单独写在
第 8 节，因为"为什么不用另一个方案"比"用了哪个"更容易被后来的改动破坏。

---

## 1. 分层与依赖方向

```
            ┌──────────────────────────────────────────┐
            │  ui/          PySide6 桌面端              │
            │  只依赖 contract + 少量 engine 门面        │
            └───────────────┬──────────────────────────┘
                            │ 命令（commands）/ 事件（events）
            ┌───────────────▼──────────────────────────┐
            │  contract/    纯数据：枚举 / 模型 / 端口    │
            │  零第三方依赖，引擎与 UI 的唯一共享词汇     │
            └───────────────┬──────────────────────────┘
                            │ 实现
            ┌───────────────▼──────────────────────────┐
            │  engine/      扫描 / 规划 / 调度 / 流水线   │
            └───────────────┬──────────────────────────┘
                            │ 块读写（BlockDevice 协议）
            ┌───────────────▼──────────────────────────┐
            │  devices/     内存 / 镜像文件 / Windows 原始卷│
            └───────────────┬──────────────────────────┘
                            │ 卷枚举、错误分类、提权
            ┌───────────────▼──────────────────────────┐
            │  platform/    ctypes 封装，唯一的平台相关层  │
            └──────────────────────────────────────────┘
```

依赖**只向下**。由此得到三个可验证的结论：

* `engine` 里 import 不到 `PySide6`（可以 grep 验证），所以引擎能在无 GUI 的
  CI 上跑完整流程；
* `contract` 里 import 不到 `engine`/`devices`，所以 UI 能用假引擎单测；
* 平台差异全部收敛在 `devices/win32.py` 与 `platform/`，引擎里没有
  `if sys.platform`。

---

## 2. 目录结构与职责

```
src/lbamover/
├── __init__.py          版本号（三段 + 字母 revision 分离）
├── __main__.py          python -m lbamover 入口
├── cli.py               无 GUI 入口：volumes / selftest / run / gui
├── contract/            ← 引擎与 UI 的契约层
│   ├── enums.py         JobState 状态机、Strategy、ErrorClass、VolumeKind
│   ├── models.py        Extent / FileFrag / FragReport / Bucket / MoveOp /
│   │                    Plan / JobParams / Progress / VolumeInfo
│   ├── events.py        引擎→UI 的 6 类不可变事件
│   ├── commands.py      UI→引擎的 8 条不可变命令
│   └── ports.py         BlockDevice / FileCatalog / EventSink / JobJournal 协议
├── engine/              ← 业务核心，不依赖 Qt
│   ├── chunk.py         把 MoveOp 切成 BlockSlice（目标地址连续推进）
│   ├── frag.py          碎片指标 + 等宽分桶（可视化用）
│   ├── alloc.py         IntervalSet（禁区集合）+ FreeSpaceMap（first-fit 分配）
│   ├── planner.py       三个策略 → 一串 MoveOp；静态自检 validate_plan
│   ├── pipeline.py      单 op 的读/写重叠流水线（有界队列）
│   ├── scheduler.py     并发池 / 令牌桶限速 / 断点落盘 / 结果汇聚
│   ├── retry.py         指数退避 + 抖动 + 错误分类
│   ├── control.py       取消 / 暂停 / 可中断等待
│   ├── checkpoint.py    原子断点 + JSONL 流水
│   ├── progress.py      滑动窗口速率 + ETA + 进度快照
│   ├── catalog.py       文件目录服务（Static / Json / Unsupported）
│   ├── synth.py         确定性合成卷（造碎片、填充、逐块校验）
│   ├── errors.py        Cancelled / VerifyError / PlanRejected
│   └── engine.py        EngineCore（同步门面）+ EngineService（线程宿主）
├── devices/             ← LBA 块读写
│   ├── base.py          BlockDeviceBase 契约 + DeviceSpec + open_device
│   ├── memory.py        内存盘（带故障注入：短读/失败/写坏/延迟）
│   ├── image.py         镜像文件盘（pread/pwrite 语义，短读循环补齐）
│   └── win32.py         原始卷读写（对齐缓冲）+ FSCTL_MOVE_FILE + 能力探测
├── platform/            ← 平台细节
│   ├── volumes.py       卷枚举（只读探测，普通权限也能列出目标）
│   ├── privilege.py     管理员检测 / 特权启用 / runas 重启
│   └── errors.py        错误码 → 可重试 / 不可恢复
├── packaging/           ← 封包（不参与运行时）
│   ├── build.py         一键封包 + 产物验证
│   ├── make_icon.py     用项目色板画多尺寸 .ico
│   ├── make_version_file.py  exe 版本资源（版本号唯一来源）
│   ├── gui_entry.py     窗口版入口（console=False）
│   ├── cli_entry.py     命令行版入口（console=True）
│   └── lbamover.ico     产物图标（生成物，已入库）
└── ui/                  ← 桌面端
    ├── app.py           QApplication + 主题 + 主窗口装配
    ├── bridge.py        EventSink → Qt 信号（唯一的 QObject 接触点）
    ├── theme.py         色板 + QSS + 字体回退
    ├── format.py        字节/速率/ETA 的统一格式化
    ├── main_window.py   信息架构与响应式布局
    └── widgets/         卡片、卷选择、分布图、进度卡、速率曲线、日志、参数、控制栏
```

---

## 3. 数据流转

### 3.1 扫描流（不写盘）

```
VolumePicker 选中卷
   → UpdateTarget(volume, params, strategy)
   → [engine 线程] EngineCore.set_target → EngineCore.scan
        → FileCatalog.list_files / free_extents
        → engine.frag.analyze：规范化区间 → 分桶 → 出指标
        → FragMapEvent
   → [GUI 线程] FragMap.set_report / 指标块 / 最值得整理列表
   → EngineCore.make_plan（同一命令内跑，保证预览与执行参数一致）
        → PlanEvent
```

### 3.2 执行流（写盘）

```
ControlBar「开始整理」
   → 危险确认（系统卷要求手输盘符）
   → StartJob(JobSpec)
   → [engine 线程] scan → make_plan → validate_plan
        → JobRunner.run(plan, resume)
             ├─ 已完成 op 直接跳过（计入进度）
             ├─ ThreadPoolExecutor(max_workers = 1 若 requires_serial)
             │     └─ 每个 op → ChunkPipeline.run_op
             │           生产者线程：read_blocks（带重试）
             │                    ↓ 有界队列（深度 = queue_depth）
             │           消费者线程：write_blocks + 可选回读校验 + 限速
             ├─ 每个 op 完成 → 断点原子落盘 + JSONL 流水
             └─ 每 N 个 chunk → 进度事件（节流 12.5 Hz）
   → ProgressEvent / JobDoneEvent
   → [GUI 线程] 进度卡 / 速率曲线 / 日志
```

### 3.3 线程模型

| 线程 | 数量 | 职责 |
| --- | --- | --- |
| GUI 线程 | 1 | 只画界面、只发命令；不做任何 IO |
| engine 线程 | 1 | 串行消费命令队列，跑扫描/规划/执行 |
| op 工作线程 | 1..concurrency | 每个 op 一条流水线 |
| 读线程 | 每 op 1 条 | 预取 chunk 进有界队列 |

暂停/继续/停止**不走命令队列**——它们在线程外直接作用于 `RunControl`。
否则任务跑起来后队列没人消费，暂停命令会永远排队（旧版"取消没反应"的根因）。

---

## 4. 核心算法

### 4.1 分块映射（`chunk.py`）

一个 op 的多个源 extent 在盘上不连续，搬到目标时**紧密拼接**：

```
目标地址 = op.dst_start + 该 op 内已累计的块数      ← 不是"源地址 + 位移"
```

写成后者是最常见的实现错误，且只有多 extent 用例会暴露。`slice_move_op`
的返回值保证 `dst_lba` 单调且无缝。

### 4.2 碎片指标（`frag.py` / `models.py`）

| 指标 | 定义 | 为什么要这个 |
| --- | --- | --- |
| `FileFrag.frag_score` | `1 - 最大段 / 总块数` | 对"大文件被切成两半"敏感，对"一堆小段里最大那段很大"不过度惩罚 |
| `FragReport.file_frag_score` | 按块数加权平均 | 避免 100 个 1KB 小文件把指标拉满 |
| `FragReport.free_frag_score` | `1 - 最大连续自由区 / 自由块数` | 「还能不能放下一个大文件」的直接指标 |
| `Bucket.frag_ratio` | 桶内 extent 数 / 桶长，按"每 4 块一个边界"为满值 | 只用于画图；标定成 1/8 会让任何桶一有 extent 就 25% 热，整条带变成均匀底色 |

只有**文件**的 extent 参与密度统计。自由区通常是一整块大 extent，让它计入
会给每个桶都 +1，热力带变成均匀底色，信息量为零。

### 4.3 规划不变量（`planner.py` / `alloc.py`）

> 任何 op 的目标区，都不得覆盖**尚未搬走**的文件的源区。

违反它的后果不是效率低，而是读到一半的源数据被自己的写入覆盖——**永久丢失**。

实现方式：

1. 维护 `blocked`（还没搬的文件占的块）。处理某个文件前先把它自己从
   `blocked` 中**按区间**挖掉（不是丢掉整段，合并后的段常横跨多个文件）；
2. 分配时 `avoid = blocked ∪ 该文件自己的全部 extent`——自己的源区也不能当
   目标，否则自我覆盖；
3. 分配成功后把它的旧 extent 释放回自由空间，后续 op 可以复用；
4. 若任一 op 的目标落在**另一个 op 的源区**上，置 `Plan.requires_serial=True`，
   调度器强制单线程执行。这是正确性要求，不是性能妥协。

`validate_plan()` 在执行前再查一遍：越界、自我覆盖、目标互相重叠、以及
"声明可并行却存在目标踩源"。有问题直接 `PlanRejected`，一块都不写。

### 4.4 流水线（`pipeline.py`）

```
生产者线程                        消费者（工作线程）
read chunk N   ──► 队列(深度 D) ──► write chunk N + 校验
read chunk N+1 ──►              ──► write chunk N+1
```

* **队列有界**：在途数据 ≤ `chunk_blocks × block_size × queue_depth`。
  无界队列会让 2TB 的卷把内存吃满——这是"假流式"最常见的形态；
* **顺序保持**：断点记录"已写前 K 块"依赖这一点；
* **取消后排空队列**：直接退出会让生产者永久阻塞在 `put()`，线程泄漏。

### 4.5 断点（`checkpoint.py`）

* 落盘用「临时文件 + `os.replace`」，掉电不会留下半截 JSON；
* 断点带 `plan_hash`，计划变了就拒绝续做——用旧断点会把「A 位置写完」
  错当成「B 位置已完成」，属于静默数据损坏；
* 损坏的断点文件改名 `.bad` 留证，不静默覆盖；
* 流水是 JSONL 追加，尾部半行跳过、不影响前面的记录。

### 4.6 重试（`retry.py`）

* 只重试 `ErrorClass.RETRYABLE` / `VERIFY_MISMATCH`；
* 未知错误码一律 `FATAL`——"多做一次写"的代价可能是数据损坏；
* 指数退避 + ±35% 抖动，避免多个 worker 同步撞击故障点；
* 有上限：无上限重试会把"任务失败"变成"任务永不结束"。

### 4.7 调度与限速（`scheduler.py`）

* 并发 = `1` 若 `plan.requires_serial`，否则 `params.concurrency`；
* 令牌桶限速，桶容量至少一次请求量（否则小桶 + 大 chunk 会永久空转——
  这条是测试抓出来的真实缺陷）；
* 每个 op 结束落断点；全部成功且无失败才清断点（`EngineCore.execute`）。

---

## 5. 内存模型

内存上界只由参数决定，与卷大小无关：

```
buffer_budget = chunk_blocks × block_size × queue_depth × concurrency
```

`JobParams.buffer_budget_bytes` 就是这个乘积，UI 的参数面板实时显示它。
默认值（256 × 4KiB × 4 × 2）= 8 MiB。

分桶可视化也不构造 per-block 数组：1TB / 4KiB 有 2.6 亿块，任何
"每块一个元素"的实现都会直接 OOM。`bucketize` 一次遍历把 extent 摊到桶里。

---

## 6. 与 UI 的接口

### 6.1 命令（UI → 引擎，8 条）

| 命令 | 语义 |
| --- | --- |
| `UpdateTarget` | 换目标卷，可选「扫描 + 规划」预演 |
| `StartJob` | 开始整理（带 `JobSpec` 时重新扫描并规划） |
| `PauseJob` / `ResumeJob` / `CancelJob` | 直通 `RunControl`，不进队列 |
| `SetParams` | 热更新：限速、回读校验 |
| `Shutdown` | 引擎线程退出 |

### 6.2 事件（引擎 → UI，6 类）

`StateEvent` / `ProgressEvent` / `LogEvent` / `FragMapEvent` / `PlanEvent` /
`JobDoneEvent`。全部不可变快照，由 `ui/bridge.py` 翻译成 Qt 信号。

`EngineBridge` 是**唯一** import `QObject` 的地方（引擎侧完全不知道 Qt 存在）。

---

## 7. Windows 真机后端的边界（重要）

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 卷枚举 / 容量 / 文件系统 | ✅ 已实现 | 普通权限即可 |
| 原始卷块读 | ✅ 已实现 | `\\.\X:` + 页对齐缓冲 + 短读补齐 |
| 原始卷块写 | ⚠ 有条件 | 必须先 `FSCTL_LOCK_VOLUME`；对**已挂载**卷裸写必然破坏文件系统 |
| `FSCTL_MOVE_FILE` 原语 | ✅ 已实现 | 由 NTFS 自己搬簇并更新元数据，这才是安全路径 |
| 实时 NTFS 文件枚举（MFT / retrieval pointers） | ❌ **未实现** | 缺这一步就没有"文件 → LBA"的映射，真机扫描会明确报能力不足 |
| 卷镜像离线整理 | ✅ 可用 | `ImageDevice` + `JsonCatalog` + `lbamover run` |

**为什么不做静默降级**：拿不到文件表时返回空报告，界面上会显示"0 个碎片
文件，卷很健康"——而事实是根本没看过。这类假象比报错危险得多。

`devices/win32.capabilities()` 会把探测结果如实上报到界面顶部的徽标，
用户能直接看到"为什么点了没反应"。

---

## 8. 取舍记录

| 决定 | 备选 | 为什么这么选 |
| --- | --- | --- |
| 契约层用 frozen dataclass + Protocol | 直接共享引擎对象 | 引擎多线程读写，共享可变状态必然出竞态；frozen 让"事件是快照"成为编译期约束 |
| 规划按**文件**而不是按块 | 纯块级搬运 | 块级自由搬运会把文件拆得更碎，且没有文件边界就无法判断"搬完是否成功" |
| 目标区只用 first-fit | best-fit | 整理的目标本身就是往低地址聚拢；best-fit 会把文件撒开，反而更碎 |
| 断点原子替换而非追加 | 直接覆写 JSON | 掉电半截文件 = 断点作废，等于没做断点 |
| 取消/暂停绕过命令队列 | 全部走队列 | 队列由正在执行任务的线程消费，任务跑起来后没人读队列 |
| 单窗口单状态机 | 每功能一套 worker | 旧版 8 套 worker 各自判定生命周期，演化出"取消没反应/按钮永久禁用"一族缺陷 |
| 中央区可滚动 | 强制压缩布局 | 压缩会让卡片低于最小高度、子控件互相重叠；滚动是诚实的降级 |
| 真机后端默认拒绝写已挂载卷 | 直接允许 | 那是必然导致文件系统损坏的操作，不是"可能有风险" |

---

## 9. 测试策略

| 层 | 覆盖内容 | 数量 |
| --- | --- | --- |
| 契约层 | 区间规范化、指标定义、计划指纹、参数校验 | 39 |
| 分配/规划 | 不变量（目标不踩源）、串行判定、静态自检 | 30 |
| 流水线 | 多 extent 拼接、短读、重试、取消不挂死、非假流式 | 20 |
| 调度/断点 | 原子性、损坏隔离、续做跳过、并发、限流 | 22 |
| 设备层 | 短读契约、越界、只读、镜像往返 | 33 |
| 引擎 | 状态机、事件、三策略端到端 + 逐块校验、目录绑定 | 31 |
| UI | 事件→信号映射、按钮状态矩阵、自绘控件渲染、窗口装配 | 32 |

外加 `scripts/smoke.py`：引擎端到端 + GUI 装配 + 通过引擎线程驱动一次
真实搬迁并逐块校验。**pytest 证明不了"窗口能起来"，冒烟才能。**

数据正确性的判定方式只有一个：合成卷里每块内容是 `(seed, 文件, 块序号)` 的
哈希，搬完之后**逐块**比对。`apply_plan()` 先算出「执行后的布局」再校验——
按旧布局校验只会得到一片假失败。
