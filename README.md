# LBAMover 2.0

**把一个文件的碎片，搬成连续的一段。**

Windows 上的 NTFS 卷用久了，文件会被切成几十上百个碎片散在盘上。LBAMover
以 **LBA 块**为最小单位做这事：先在内存里算出「哪个文件的哪几段该搬到哪」，
再用文件系统自己的搬簇接口落盘，全程有保护层、有断点、有逐条失败原因。

```
扫描卷 → 规划搬迁 → 审计 → 搬簇 → 复核
```

Python 3.11+ · Windows x64 · 引擎零第三方依赖 · 界面 PySide6

---

## ⚠️ 先读这一段

**这个工具会改动磁盘上的块。** 用之前请接受这三件事：

1. **先备份。** 任何声称"安全"的整理工具都不该被盲信，包括这个。
2. **它只走 `FSCTL_MOVE_FILE`** —— 让 NTFS 自己搬簇并更新元数据，不走裸写。
   裸写已挂载的卷必然破坏文件系统，那条路在代码里默认拒绝。
3. **第一次上真机前，先拿镜像验收**：
   ```bash
   lbamover ntfs --image dump.img   # 解析文件表，确认识别得对
   ```

系统卷（C:）执行整理要求**手输盘符**确认，没有"回车即通过"的路径。

---

## 拿到它

### 方式一：直接用（自己封一个 exe）

仓库不随代码分发二进制。封包脚本一条命令，产物在 `dist/`：

```bash
pip install -r requirements.txt
python packaging/build.py            # 图标 → 版本资源 → PyInstaller → wheel → zip → 校验和
```

| 产物 | 说明 |
| --- | --- |
| `dist/LBAMover/LBAMover.exe` | 图形界面，双击即用（单一 exe 3 MB，运行库在 `_internal/`） |
| `dist/LBAMover/LBAMover-cli.exe` | 命令行，`--version` / `volumes` / `scan` / `selftest` / `smoke` |
| `dist/LBAMover-2.0.0E-win64.zip` | 解包即用，约 48 MiB |
| `dist/lbamover-2.0.0-py3-none-any.whl` | `pip install` 用 |
| `dist/SHA256SUMS.txt` | 上述产物的校验和 |

脚本**会自己验证产物**（版本号一致 / 合成卷端到端 / 两个 exe 各自无头跑一次
完整搬迁），任一项失败即中止退出。源码树里测试全绿不代表 exe 能起来。

> 未做代码签名，首次运行有 SmartScreen 提示。介意就用 `SHA256SUMS.txt` 核对。

### 方式二：从源码跑

```bash
pip install -r requirements.txt
PYTHONPATH=src python -m lbamover gui --demo
```

`--demo` 会在内存里造一个 32 MiB 合成卷，**全程不碰真实磁盘**，用来先看看界面
长什么样、流程怎么走。

---

## 三种用法

### 图形界面

```bash
PYTHONPATH=src python -m lbamover gui
```

左栏选卷 → 选策略 → 预演 → 开始。中央是碎片分布双带图、指标块、最该整理的文件、
进度与吞吐曲线；底部是可过滤的日志和固定操作栏。

- **整理范围**：整卷，或手动勾选（支持搜索 / 只看碎片 / 按段数或大小排序）。
  空选择会被锁住——引擎里"空列表"等于整卷，这个歧义不能留在界面上。
- **权限徽标**：标题栏显示当前权限。非管理员时点一下就是「以管理员身份重启」。
  这不只是提示——**未提权只能预览**（下面解释），所以它是能不能真干活的分界线。
- 窗口窄了左栏自动收起，窗口矮了出滚动条，不会把卡片压到互相重叠。

### 命令行

```bash
lbamover volumes                      # 本机可整理的卷
lbamover scan --volume D               # 扫描真实卷，打印碎片分布（诊断「扫不动」用）
lbamover selftest --blocks 4096 --files 24   # 合成卷全链路 + 逐块校验，成功输出 [PASS]
lbamover ntfs --image dump.img --make-demo   # 没有镜像就先生成一份结构合法的合成 NTFS
lbamover run --image disk.img --layout layout.json --strategy compact   # 对镜像做离线演练
lbamover smoke                        # 无头冒烟：建窗口 + 跑完一次真实搬迁
```

### 当库用

```python
from lbamover.engine import EngineCore
# 引擎不依赖 Qt，可以在无 GUI 环境（CI、脚本、服务）跑完整流程
```

引擎与界面之间只有 `contract/` 一层：9 条命令、6 类事件、若干 Protocol。
`ui/` 之外没有任何文件 import PySide6。

---

## 能做什么，不能做什么

诚实版，不含"即将支持"。

| 能力 | 状态 |
| --- | --- |
| 块级流式搬运（内存盘 / 镜像文件） | ✅ 逐块校验通过，内存上界与卷大小无关（默认 8 MiB） |
| NTFS 文件表解析（`$MFT` → 文件 → 簇区间） | ✅ 数据卷上跑通：3937 文件 / 0.8s |
| 簇级搬迁（`FSCTL_MOVE_FILE`） | ✅ 代码完整，⚠ **尚未在真实卷上执行过** |
| 保护层（元文件 / 系统目录 / 页面文件 / 空闲余量） | ✅ 规划前过滤 + 执行前二次审计 |
| 三个动作策略：合并碎片 / 向卷首聚拢 / 搬到卷尾 | ✅ 外加「只分析」零写入模式 |
| **可用空间整理** | ✅ 按 GiB 指定大小，选卷首或卷尾。只承诺"这段连续空闲确实是空的"——做不到就报差额，不给"搬了一半"的结果 |
| **手动选文件** | ✅ 只整理勾选的，选择身份是文件路径而非行号（跨扫描稳定） |
| **失败原因 + 一键复制诊断** | ✅ 每条失败带阶段、异常类型、错误分类、Windows 错误码；日志面板把版本与明细打包成一段可直接粘贴求助的文本 |
| **结局三档** | ✅ 已完成 / 部分完成 / 失败。19 成功 1 失败不会再显示成绿色"已完成" |
| 并发 / 限速 / 暂停 / 停止 / 断点续做 | ✅ 断点带计划指纹，计划变了拒绝续做 |
| **未提权扫描** | ⚠ 只读预览。走目录遍历 + 逐文件簇映射（不需要权限），但拿不到 `$Bitmap`、看不到打不开的文件 → 界面锁住「开始整理」 |
| **MFT 扫描（提权后）** | ✅ 88 万文件约 20s（未提权约 150s）。权限徽标就是性能开关 |
| exFAT / FAT32 | ⚠ 只读预览，不支持执行 |

扫描速度快慢的分层原因、位图与 MFT 的实现细节、为什么并发只有 1.6x：
见 [`docs/ARCHITECTURE.md` §7.1](docs/ARCHITECTURE.md)。

---

## 开发

```bash
python tools/testflow.py        # 唯一测试入口，只看 summary 一行
PYTHONPATH=src python scripts/smoke.py    # 全链路冒烟（GUI 装配 + 经引擎线程真实搬迁）
python scripts/shots.py         # 界面截图（视觉评审）
```

测试断言的是数据正确性：搬完之后用 `synth.apply_plan()` 算出预期布局，再逐块哈希
比对。`INVALID` 表示跑的过程中受版本控制或源码文件被改动，成败都不作数，只许重跑。

| 目录 | 职责 |
| --- | --- |
| `src/lbamover/contract/` | 枚举 / 模型 / 命令 / 事件 / 端口。引擎与 UI 的唯一共享词汇 |
| `src/lbamover/engine/` | 扫描 / 规划 / 调度 / 流水线 / 保护层 / 断点 |
| `src/lbamover/ntfs/` | boot / runlist / record / catalog / bitmap / synth |
| `src/lbamover/devices/` | 内存 / 镜像文件 / Windows 原始卷 |
| `src/lbamover/platform/` | ctypes 封装，唯一的平台相关层 |
| `src/lbamover/ui/` | PySide6 桌面端 |
| `docs/ARCHITECTURE.md` | 目录职责、数据流、算法、取舍记录、真机边界 |

依赖**只向下**：`ui → contract ← engine → devices → platform`。由此保证引擎能在无
GUI 的 CI 上跑完整流程，UI 能用假引擎单测，平台差异没有渗进引擎。

---

## 版本号约定

`pyproject.toml` 的 `version` 永远只写三段（`2.0.0`，PEP 440 合法），小版本字母放在
`[tool.lbamover].revision`，由代码拼成 `2.0.0E` 对外展示。

`2.0.0E` 不是合法 PEP 440，pip 会直接拒绝——所以字母不能进 `version` 字段。
两处必须一致，`tests/test_version.py` 盯着这件事。
