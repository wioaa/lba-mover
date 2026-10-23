# LBA Tail Mover — 原理与说明（manual / datasheet）

> 本文件放**原理与深入说明**（架构、Win32 契约、算法、安全模型、各策略原理）。
> 快速上手、运行方式、安全须知、界面操作、已知限制见 [README.md](README.md)；
> 版本变更记录见 [CHANGELOG.md](CHANGELOG.md)。

---

## 1. 总体架构与分层

```
app/
  win32/   全部 ctypes / Win32 调用（只在这一层出现；不依赖 pywin32）
  core/    纯业务逻辑（模型、扫描、规划、调度），纯函数 + dataclass，可单测
  ui/      PySide6 界面层，只连 core 的信号，不碰 ctypes
```

* **唯一写路径**：`app/win32/mover.py` 的 `FSCTL_MOVE_FILE`，只在
  `MoveWorker.run()` 内部**按需导入**；因此「预览模式」代码路径上不存在任何写调用点
  （可验证：`import app.ui.main_window` 后 `'app.win32.mover' in sys.modules` 为 `False`）。
* **唯一原始读点**：`app/win32/mft_reader.py`（直读 `$MFT`），严格只读。
* 耗时操作（位图 / 扫描 / 索引 / 规划 / 移动 / 分块流式）都在 `QThread` 中执行。

### 文件结构

```
lba-mover/
  main.py                    入口：高 DPI、单实例互斥、异常兜底、--selftest、全局 QSS
  app/
    win32/
      api.py                 kernel32/advapi32/shell32 绑定、DeviceIoControl 双封装、Win32Error
      volume.py              卷枚举、GetVolumeInformation、FSCTL_GET_NTFS_VOLUME_DATA、read_volume_serial
      bitmap.py              FSCTL_GET_VOLUME_BITMAP 分块读取、popcount、空闲区间提取
      retrieval.py           FSCTL_GET_RETRIEVAL_POINTERS 变长结构解析
      storage.py             IOCTL_STORAGE_QUERY_PROPERTY 介质探测（HDD/SSD，只读）
      mft_reader.py          $MFT 直读（唯一原始读点，严格只读）
      mover.py               FSCTL_MOVE_FILE（唯一写操作出口）
      file_io.py             用户态 Read/Write 分块流式搬迁 I/O + 回收站删除（1.0.0C）
      privilege.py           管理员检测 + SeManageVolumePrivilege 提权
    core/
      models.py              VolumeInfo / FileEntry / Extent / MovePlan 等 dataclass
      ranges.py              纯区间工具（merge / subtract / run_length / ceil_div）
      safety.py              安全保留区策略 + 硬下限夹紧 + 安全论证文案
      guard.py               逐次目标校验（位图 + 保留区 + 禁区 + 越界）
      ownership.py           LCN 区间 → 文件 的归属索引（N4/N5 共用）
      io_profile.py          介质策略 / 批次换算 / 段合并 / 预热参数
      packer.py              FDD 装箱 + 空间账本 + 挡路者识别 + 可行性三档
      scheduler.py           依赖图 + 电梯序（Kahn + 最小堆）+ 环断裂（relay）
      journal.py             JSONL 流水（逐段 flush），手动回滚用
      mft.py                 FILE 记录解析（fixup / 属性链 / runlist / 路径）
      planner.py             末端分配（v1）+ 区间装箱 build_range_plan（v2）
      densify.py             1.0.0C 密实化策略 + 卷前半大文件扫描（纯函数）
      block_stream.py        1.0.0C 分块流式调度（65536 簇 block / 可调缓存 / 流水线）
      scanner.py             位图/扫描/索引/规划/移动/分块流式 QThread Worker
    ui/
      theme.py / icons.py / cards.py / title_bar.py / disk_map.py / file_table.py
      log_panel.py / dialogs.py / target_panel.py / io_panel.py / scan_panel.py
      result_panel.py / widgets.py / main_window.py
  tools/
    testflow.py / TESTFLOW.md / run_all_tests.py / preview_shots.py / version_info.txt
  tests/                    单测（自带 _harness，python tests/test_xxx.py 亦可单跑）
  docs/screenshots/         界面截图
```

---

## 2. 关键 Win32 调用（契约与验证）

| 调用 | 控制码 / 结构 | 用途 | 验证结果 |
| --- | --- | --- | --- |
| `FSCTL_GET_NTFS_VOLUME_DATA` | `0x00090064`，`NTFS_VOLUME_DATA_BUFFER`（96 B） | 簇大小 / 总簇数 / MFT 区 | R: 实测 19,660,799 簇、4 KiB 簇、MFT 区 `[89440, 129088)` |
| `FSCTL_GET_VOLUME_BITMAP` | `0x0009006F`，`VOLUME_BITMAP_BUFFER`（16 B 头 + 位图） | 卷占用位图（分块拼接） | R: 2,457,600 字节位图；占用 2,158,869 簇、空闲 17,501,930 簇，与 `FreeClusters` **完全一致** |
| `FSCTL_GET_RETRIEVAL_POINTERS` | `0x00090073`，变长 `RETRIEVAL_POINTERS_BUFFER` | 文件 VCN→LCN extents | `ntoskrnl.exe` 解析出 2 段（VCN 0→1388 @LCN 18911832、1388→3206 @LCN 70736138） |
| `FSCTL_MOVE_FILE` | `0x00090074`，`MOVE_FILE_DATA`（**sizeof = 32**） | 真实移动簇 | 偏移校验：`FileHandle@0`、`StartingVcn@8`、`StartingLcn@16`、`ClusterCount@24`（验证中未执行真实移动） |
| `AdjustTokenPrivileges` | `SeManageVolumePrivilege` | 提权 | 非管理员时正确返回失败并给出提示 |
| `GetVolumeInformationW` | `VolumeSerialNumber`（DWORD） | 卷序列号（执行前同卷校验） | 与 FSCTL 的 64 位序列号**低 32 位**一致（见 §4 同卷校验） |
| `CreateFileW / ReadFile / WriteFile` | 用户态文件 I/O | 分块流式搬迁（1.0.0C） | 对临时文件往返验证（tests/test_file_io.py） |
| `SHFileOperationW` | `FO_DELETE \| FOF_ALLOWUNDO` | 源移回收站（可恢复） | 对临时文件验证（回收站、非永久删） |

补充：`MOVE_FILE_DATA` 的 `ctypes.sizeof` 为 **32 字节**，字段偏移与 `winioctl.h` 一致；
`NTFS_VOLUME_DATA_BUFFER` 按 `"<qqqqqIIIIqqqqq"` 解析，长度 96 字节。

---

## 3. 介质探测（N3 的原理）

`IOCTL_STORAGE_QUERY_PROPERTY` 的 `StorageDeviceSeekPenaltyProperty = **7**`
（不是 4=WriteCache、不是 6=AccessAlignment）。真实返回为 **12 字节**布局
`{Version, Size, IncursSeekPenalty}`，布尔在 **offset 8**（非 MSDN 头注释所说的 offset 4）。
`decode_seek_penalty` 只承认 `len==8`（offset 4）或 `len==12`（offset 8 且 `Size==12`、
`Version>0`）两种布局，其余（28 / 24 / 64 …）返回 `None`——**绝不猜**，`err=1`
（`ERROR_INVALID_FUNCTION`）也归 `unknown`。宁可保守判 `unknown`，也不把 HDD 误判成 SSD
（那会让它静默失去全部加速）。

---

## 4. 安全模型（原理）

* **三重几何禁区**：卷首保留区（默认 16 MiB、硬下限 1 MiB，`clamp_reserve_mb` 用
  `ceil_div` 唯一换算、不放大）、卷尾保留区（末端 LBA 防护）、MFT 保留区
  （读 `MftZoneStart/End`，绝不把簇搬进 `$MFT`）。
* **对象禁区**（跳过并标注不可移动）：`$` 开头的 NTFS 元数据、压缩（WOF/LZX）、
  EFS、无物理分配（`resident $DATA`）。
* **双重校验**：规划阶段 `planner` 断言区间合法；执行阶段每次 `FSCTL_MOVE_FILE` 前由
  `SafetyGuard.allow()` 再核一次（位图空闲 + 保留区 + 禁区 + 不越界）。
* **同卷校验（TOCTOU 防护）**：执行前重读卷身份——序列号用 `GetVolumeInformation`
  （与规划期同源，都是 32 位 DWORD；**不能**拿规划期 32 位去比执行期 FSCTL 的 64 位
  `VolumeSerialNumber`，否则同一卷会误报「序列号已变化」），总簇数用 FSCTL（两边同源）。
* **分区表物理不可达**：只在卷内 LCN 层面移动，**从不打开 `\\.\PhysicalDriveN` 可写**。
* **预览无写路径**：见 §1 的按需导入隔离。

---

## 5. 迁移规划算法（原理）

`app/core/planner.py` 是纯函数模块，输入输出都是 dataclass / 区间列表。

**v1 末端模式**（`build_plan`）：
1. `floor = TotalClusters - ReserveClusters`；
2. 位图提取 `[floor, TotalClusters)` 内空闲连续区间（64 KiB 分块 + 大整数位运算）；
3. 从卷尾向前、按「文件簇数降序」依次放置，每个文件要求**连续**落位；
4. 跳过被占用簇与 MFT 保留区；放不下**报错而非硬塞**；
5. 已整段位于预留区的文件标记「已就位」，不再搬动；
6. 输出 `MovePlan`（源 extents、目标起始 LCN、逐段 `MoveSegment`）。

**v2 区间模式**（`build_range_plan`）：`packer.fdd_pack` 把选中文件连续装入目标区间 →
`find_blockers` 识别挡路者 → `build_evictions` 迁出 → 可行性三档（`direct` / `evict` /
`impossible`，量化原因）→ `scheduler` 依赖图 + 电梯序 + Tarjan SCC 环断裂（relay）。

---

## 6. v2 引擎层实测数据（只读验证，无真实 FSCTL_MOVE_FILE）

### MFT 直读通道 vs 句柄通道

| 卷 | MFT 通道 | 句柄通道 | 比值 | 记录数 | 在用记录 | 文件 |
| --- | --- | --- | --- | --- | --- | --- |
| `R:`（≈75 GiB） | **0.31 s** | 0.68 s | **2.2×** | 46,592 | 5,363 | 5,143 |
| `F:`（≈1.8 TiB） | **19.8 s 热 / 79.9 s 冷** | 未跑全量（225 k 文件） | — | 1,587,456 | 225,036 | 约 21 万 |

句柄通道必须逐文件 `CreateFile` + IOCTL，权限失败 / 被独占的文件会被跳过（`R:` 少 1,206 个）；
MFT 通道能列出**全部**登记在案的文件（含被独占、压缩、EFS），对它们做「可搬性」标注而非隐藏。

### 抽样复核（默认 32 个/次）

按 `(vcn, lcn, clusters)` 三元组与句柄通道比对（只比总量会漏掉「vcn 错位导致文件中段被搬走」
这类静默错乱）；`R:` 32/32、`F:` 32/32 一致；不一致率 > 5% 时整条 MFT 通道自动降级并留痕。

### 自动回退原因码（不允许静默降级）

`not_ntfs` / `no_privilege` / `mft_runlist_unavailable` / `structure_invalid` /
`bounds_violation` / `sample_mismatch` / `user_preferred_handle`——各自有用户可见中文文案。

### `$MFT` 自身 runlist 的取法

首选 `CreateFile("X:\$MFT")` + `FSCTL_GET_RETRIEVAL_POINTERS`（不解析引导扇区）；
非提升进程 / 安全软件策略下会被拒（`ERROR_ACCESS_DENIED`），此时改用**卷设备句柄 +
`MftStartLcn`** 读 `$MFT` 记录 0 取其 runlist（仍只读、仍不解析引导扇区）。两条都不行才回退句柄通道。

---

## 7. v2 用户功能（N1–N5）原理

* **N1 磁盘安全保留范围**：见 §4 安全模型。
* **N2 LBA 万向移动（区间装箱）**：见 §5 v2 区间模式；自重叠 / 依赖环用 relay 两阶段
  （`src → relay → dst`，最多 3 轮环断裂）解决，而非靠普通落点硬塞。
* **N3 内存加速（可调缓冲）**：`FSCTL_MOVE_FILE` 在内核搬数据，用户态缓冲无意义——
  所谓加速是**诚实的 IO 调度**：介质探测（§3）+ 批次合并（单次最多 65536 簇连续段）+
  预读预热 + 电梯序（Kahn 拓扑序 + 最小堆按 `dst_lcn`）；「IO 加速」面板可调。
* **N4 挡路碎片自动整理**：`find_blockers` 用位图二次过滤（剔除陈旧索引幽灵）；
  `build_evictions` 迁出**可搬**挡路者；被独占 / `$` 元数据 / 无分配者计入
  `unknown_owner_clusters`，使可行性**诚实降级**为 `impossible`。
* **N5 MFT 扫描与安全控制**：`FILE` magic 校验 → **USA fixup**（每扇区恢复 USN）→
  属性链（含 `$ATTRIBUTE_LIST` 回退）→ runlist 解码（头字节低 4 位=长度字节数、
  高 4 位=**有符号相对偏移**，偏移-长度为稀疏段 `LCN=-1` 但 VCN 仍推进）；元数据记录
  0–15 排除、`$Root`(5) 只进路径表；每条记录边界校验，越界立即中止，绝不写一字节。

---

## 8. 1.0.0C 密实化与分块流式（原理）

三个策略 + 一个扫描 + 一个用户态分块流式引擎，均为**纯规划函数**（只算目标布局，搬运顺序 /
中转交由既有 `scheduler`），集中在 `app/core/densify.py` 与 `app/core/block_stream.py`。

### 8.1 全池自动填满末端（`densify.auto_fill_tail`）

无需手动勾选，从**全部**可移动文件里自动挑（大文件优先、要求连续落位、**放不下就跳过**）
填入末端预留区 `[total-reserve, total)` 的空闲空间，尽量填满、最大化腾出前端连续空间。
与 v1 `build_plan` 的区别：后者按用户勾选逐个放、放不下即记 failure；本策略自动选、跳过不报错。

### 8.2 选定区间预密实化（`densify.compact_range`）

把区间内**可移动**文件全部保留、向区间一端密集装箱；可打包空间 = 区间空闲 ∪ 这些文件
自身将腾出的簇（搬走即空出），再扣区间内**不可搬**障碍。搬完后碎片空闲被挤成一个
（尽量大的）连续块——即「解决空间碎片」。

### 8.3 按文件大小分区段（`densify.plan_size_zones` / `zone_plan`）

把区间按尺寸类切成连续区段（**需求优先、从大到小**：大尺寸类先拿够自己的量，超发时
小类被截断以优先保大文件），同尺寸文件聚在一段里密实排布；`zone_plan` 把结果包成
`MovePlan` 走既有计划 / 执行流程。

### 8.4 卷前半大文件扫描（`densify.front_half_report`）

把扫描到的大文件按前半卷 `[0, total//2)` 归类，挑出**占前部、应搬末端以腾出连续空间**的
候选；跨中线的按前半占比计 `front_clusters`，`min_clusters` 过滤小文件，输出
`front_occupied` / `movable_front` 汇总。UI 在扫描完成后把候选标注为「前半·候选」。

### 8.5 用户态分块流式搬迁（`block_stream` + `file_io`）

与 `FSCTL_MOVE_FILE`（簇级、元数据、需管理卷特权）不同的**特殊工作模式**：用户态用普通
`ReadFile` / `WriteFile` 把选中文件**在同卷内从前端搬到卷尾空闲区**（**无外部目的地**）。
机制（对每个选中文件）：

1. **预先填充**：按文件总大小先 `SetEndOfFile` 预分配一段（尽量连续）的簇，占住目标区
   （`file_io.preallocate_size`）；
2. **流式读出 + 内存缓存**：以 65536 簇为 1 块、缓冲按块倍数可调（`BlockBuffer`），
   把前端文件分块读入内存缓存（read-ahead 流水）；
3. **对末端流式覆盖**：把缓存块写（覆盖）进预填充的连续簇——一个分块读完立马顶上下一块，
   缓解 HDD 半双工、整体直搬更快；
4. **安全收尾**：校验 `written == size` 通过后，才把源移入回收站
   （`SHFileOperationW` `FO_DELETE|FOF_ALLOWUNDO`，可恢复）并改回原路径；删源失败则报失败
   并**保留源**，绝不半成品删。

* **两种缓存模式**（`file_io.copy_stream(mode=…, preallocate_dst=True)`）：
  `memory`（块在 RAM 缓存，最快）/ `disk`（块落暂存盘再读出，两段式流水、可缓冲更大）。
* **语义边界**：落点 LBA 由文件系统在空闲区分配——预分配保证**连续**、但不保证**精确 LCN**；
  不走 `FSCTL`、不需管理卷特权。

### 8.6 执行引擎选择（UI）

分块流式不再作为独立工作模式，而是并入侧栏「IO 加速」面板的**执行引擎**二选一
（`IoPanel.engineChanged`）：`LBA 簇级迁移`（既有全流程：规划 → 可行性 → FSCTL 精确搬）
与 `分块流式迁移`（§8.5：缓存模式 / 暂存目录 / 缓冲倍数）。主流程与左栏 UI 不变；
选中分块流式引擎后主按钮变为「开始分块流式迁移」，**免规划**、按勾选文件直接执行
（用户态，不要求管理员与卷特权），引擎切换即时联动按钮文案与可用性。

---

## 9. 打包与自检（原理要点）

* `LBA-Tail-Mover.spec`：`--onefile` + `console=False` + `uac_admin=True`（默认请求管理员）。
* `hiddenimports` 必须显式声明所有「函数内动态 import」的模块——`app.win32.mover`（唯一写路径）、
  `app.core.mft` / `app.win32.mft_reader`（MFT 双通道）、`app.win32.file_io` /
  `app.core.block_stream`（分块流式，`BlockStreamWorker.run()` 内动态 import）——否则运行期
  `ModuleNotFoundError`。
* `tools/version_info.txt`：`FileVersion` 用三段号数字四元组（`1.0.0.0`），修订字母只在
  `ProductVersion`（如 `1.0.0C`），与 `app/__init__.py` 的 `__version__` / `__revision__` 对齐。
* 自检：`LBA-Tail-Mover.exe --selftest` 逐个 import 关键模块（含上述动态 import 项），
  退出码 `0` = 打包完整。

---

## 10. 测试

* **唯一入口**：`python tools/testflow.py`（三态 PASS/FAIL/INVALID；`--quiet` 只看一行；
  `-k 子串` 只跑匹配文件）。也可 `python tests/test_xxx.py` 单跑（自带 `_harness`）。
* 覆盖：引擎纯函数（planner / packer / scheduler / mft / safety / guard / ownership /
  journal / io_profile / shadow / densify / block_stream）、Win32 契约（storage / file_io
  对临时文件真实往返 / seek-penalty 解码）、执行与回滚（executor）、UI 装配（offscreen）。
* 提交前必须跑一次并拿到 `PASS`。
