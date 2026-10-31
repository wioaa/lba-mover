# LBAMover 2.0

以 **LBA 块**为单位的流式整理引擎 + PySide6 桌面控制台。

从 0 重建，不含上一代（LBA Tail Mover / LBA Defrag Organizer）的任何代码。
旧仓库的两个回退标签 `pre-rebuild` / `legacy/1.0.0I` 保留在 `lba-mover` 原仓库，
本仓库是全新的 2.0.0 起点。

---

## 一分钟跑起来

```bash
# 依赖：Python 3.11+，UI 需要 PySide6（引擎核心零第三方依赖）
pip install -r requirements.txt

# 启动桌面端，并立刻载入一个内存合成卷（不接触真实磁盘）
PYTHONPATH=src python -m lbamover gui --demo

# 纯命令行自检：造碎片 → 扫描 → 规划 → 执行 → 逐块校验
PYTHONPATH=src python -m lbamover selftest --blocks 4096 --files 24

# 列出本机可整理的卷
PYTHONPATH=src python -m lbamover volumes

# 解析一份 NTFS 卷镜像（上真机前的验收）；没有镜像就先生成一份合成的
PYTHONPATH=src python -m lbamover ntfs --image .tmp/demo.img --make-demo

# 跑测试（唯一入口）
python tools/testflow.py

# 全链路冒烟（引擎 + GUI 装配 + 经引擎线程跑一次真实搬迁）
PYTHONPATH=src python scripts/smoke.py
```

`selftest` 成功时输出一行 `[PASS]` 并以 0 退出。

---

## 封包

```bash
python packaging/build.py            # 图标 → 版本资源 → PyInstaller → wheel → zip → 校验和
python packaging/build.py --no-wheel # 只出 exe
```

产物：

| 路径 | 说明 |
| --- | --- |
| `dist/LBAMover/LBAMover.exe` | 图形界面，双击即用，无控制台窗口（2.8 MB） |
| `dist/LBAMover/LBAMover-cli.exe` | 命令行：`volumes` / `selftest` / `smoke` |
| `dist/LBAMover/_internal/` | Python 运行时 + Qt 插件，两个 exe **共用一份** |
| `dist/LBAMover-2.0.0-win64.zip` | 分发用压缩包，47.7 MiB |
| `dist/lbamover-2.0.0-py3-none-any.whl` | `pip install` 用 |
| `dist/SHA256SUMS.txt` | 上述产物的校验和 |

目录解包后 115.7 MiB，其中 94 MiB 是 PySide6（`Qt6Core/Gui/Widgets` 加一个
19.7 MB 的 `opengl32sw.dll` 软件 OpenGL 回退），6 MB 是 CPython。

**为什么不是 onefile**：onefile 每次启动都要把 ~80 MB 解压到临时目录，冷启动
3~5 秒，杀软还会额外扫描自解压行为。这个工具经常是"双击后立刻要看到界面"，
那个代价不合适。onedir 冷启动 0.6 秒左右，代价只是"产物是一个文件夹"。

**构建脚本会自动验证产物**，任一项失败即中止（这一步不能省——源码树里
pytest 全绿不代表 exe 能跑）：

1. `LBAMover-cli.exe --version` 与代码里的版本号一致；
2. `LBAMover-cli.exe selftest` 合成卷端到端 + 逐块校验；
3. `LBAMover-cli.exe smoke` 与 `LBAMover.exe --smoke` **各自**无头建窗口并
   跑完一次完整搬迁（窗口程序没有 stdout，靠退出码汇报）。

真实平台启动校验（会弹窗，所以不进构建脚本，手工跑）：

```bash
python packaging/verify_gui_launch.py   # 5 秒后自动关闭，退出码 0 = 主窗口出现
```

已确认：`LBAMover.exe --demo` 在真实 Windows 平台创建出主窗口
（标题 `LBAMover 2.0.0 — LBA 块级流式整理`）。

> exe 未做代码签名，首次运行会有 SmartScreen 提示；内网分发可先用
> `SHA256SUMS.txt` 核对。

---

## 它做什么

**引擎**（`src/lbamover/engine`，不依赖 Qt）：

- 以 LBA 块为原子单位，把「一个文件的多个碎片区间」搬进一段连续区；
- 读/写重叠的**有界**流水线：内存上界 = `chunk × block_size × 队列深度 × 并发`，
  与卷大小无关（默认 8 MiB）；
- 碎片检测：文件碎片度、自由空间碎片度、等宽分桶热力图；
- 三个策略：合并碎片 / 向卷首聚拢 / 搬到卷尾，外加只分析（零写入）；
- 任务调度：并发池、令牌桶限速、优先级排序、`requires_serial` 强制串行；
- 断点续做：原子落盘 + 计划指纹校验，计划变了就拒绝续做；
- 错误重试：只重试可重试错误，指数退避 + 抖动，有上限；
- 进度上报：滑动窗口速率、ETA、重试/失败计数。

**界面**（`src/lbamover/ui`）：

- 左栏：目标卷 → 策略 → 参数（含「内存上界」实时显示）；
- 中央：碎片分布双带图（占用 / 碎片热力）、指标块、最值得整理的文件、
  进度卡 + 吞吐曲线；
- 底部：运行日志（级别过滤、错误横幅）与固定操作栏；
- 窄窗口自动收起左栏，中央内容靠流式布局换行；窗口过矮时出现滚动条而
  不是把卡片压到互相重叠。

两者之间只有 `contract/` 这一层：8 条命令、6 类事件、若干 Protocol。
`ui/` 之外没有任何文件 import PySide6。

---

## 能力矩阵（诚实版）

| 能力 | 状态 |
| --- | --- |
| **NTFS 文件表解析（$MFT → 文件 → 簇区间）** | ✅ 已实现：引导扇区 → 记录 0 → runlist → 全表扫描 → 路径还原 → `$Bitmap` 自由空间 |
| **簇级搬迁（`FSCTL_MOVE_FILE`）** | ✅ 已实现：文件系统自己搬簇并更新元数据，这是唯一安全的原地整理路径 |
| **保护层** | ✅ 元文件 / 系统目录 / 页面文件 / 系统属性文件排除；搬完空闲余量下限；执行前二次审计 |
| 块级流式搬运（内存盘 / 镜像文件） | ✅ 完整可用，逐块校验通过 |
| 碎片分析与分桶可视化 | ✅ |
| 三种整理策略 + 规划静态自检 | ✅ |
| 并发 / 限速 / 暂停 / 停止 / 断点续做 | ✅ |
| 卷枚举、系统卷识别 | ✅ 普通权限即可 |
| Windows 原始卷读写（离线镜像用） | ✅ 已实现（页对齐 + 短读补齐 + 锁卷），未在管理员权限下真机验证 |
| **真机 NTFS 整理全链路** | ⚠ **代码完整，但未在管理员权限的真实卷上验证过**（见下） |
| exFAT / FAT32 | ❌ 不支持（没有 MFT，本版不做） |

**上真机之前请先做这一步**：把目标分区导成镜像（`dd` / Win32DiskImager），
用 `lbamover ntfs --image dump.img` 确认文件表读得对，再动真卷。

```bash
# 没有真盘也能验收解析器：先生成一份结构合法的合成 NTFS 镜像再解析
PYTHONPATH=src python -m lbamover ntfs --image .tmp/demo.img --make-demo
```

---

## 安全须知

1. **本工具会改块**。真机路径走 `FSCTL_MOVE_FILE`（文件系统自己搬簇），
   不走裸写——裸写已挂载卷必然破坏文件系统，代码对那条路默认拒绝。
2. 系统卷执行整理要求手输盘符确认，不提供"回车即通过"的路径。
3. **保护层默认生效**：MFT 元文件、`Windows\System32` / `WinSxS` / `$Recycle.Bin`
   等系统目录、`pagefile.sys` / `hiberfil.sys` 等系统文件、带系统属性的文件，
   一律不进计划。规划阶段过滤一次，执行前再审计一次。
4. 搬完后必须至少留 5% 空闲（或卷容量的 1/20，取较大者），否则拒绝执行。
5. 演示模式（`--demo` / 「演示模式」按钮）在内存里造一个 32 MiB 合成卷，
   全程不碰真实磁盘，用于验收与演示。
6. `verify`（回读校验）默认开启。真机路径下它校验的是"搬完后文件是否真的
   变成一段连续簇"，而不是逐块比对（那需要把数据读一遍）。

---

## 目录速览

```
src/lbamover/{contract,engine,devices,ntfs,platform,ui}/   见 docs/ARCHITECTURE.md
src/lbamover/ntfs/         NTFS 解析：boot / runlist / record / catalog / synth
tests/                     290 个用例
scripts/smoke.py           全链路冒烟
scripts/shots.py           界面截图（视觉评审用）
tools/testflow.py          测试入口（零第三方依赖）
docs/ARCHITECTURE.md       目录职责、数据流、算法、取舍记录、后端边界
```

---

## 版本号约定

`pyproject.toml` 的 `version` 永远只写三段（PEP 440 合法），小版本字母放在
`[tool.lbamover].revision`，由 `lbamover/__init__.py` 拼成 `2.0.0A` 展示。
`2.0.0A` 不是合法 PEP 440，pip 会直接拒绝——所以字母不能进 version 字段。
