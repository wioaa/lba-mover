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

# 跑测试（唯一入口）
python tools/testflow.py

# 全链路冒烟（引擎 + GUI 装配 + 经引擎线程跑一次真实搬迁）
PYTHONPATH=src python scripts/smoke.py
```

`selftest` 成功时输出一行 `[PASS]` 并以 0 退出。

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
| 块级流式搬运（内存盘 / 镜像文件） | ✅ 完整可用，逐块校验通过 |
| 碎片分析与分桶可视化 | ✅ |
| 三种整理策略 + 规划静态自检 | ✅ |
| 并发 / 限速 / 暂停 / 停止 / 断点续做 | ✅ |
| 卷枚举、系统卷识别 | ✅ 普通权限即可 |
| Windows 原始卷**读** | ✅ 已实现（页对齐 + 短读补齐），未在真机管理员权限下验证 |
| Windows 原始卷**写** | ⚠ 需管理员 + 必须先锁卷；对已挂载卷裸写会**破坏文件系统**，代码默认拒绝 |
| `FSCTL_MOVE_FILE`（NTFS 安全搬簇） | ✅ 原语已实现，待接入文件枚举 |
| 实时 NTFS 文件枚举（MFT / retrieval pointers） | ❌ **未实现**（下一版的第一优先级） |
| 卷镜像离线整理 | ✅ `lbamover run --image ... --layout ...` |

拿不到文件表时**不会**返回空报告。空报告会让界面显示"卷很健康"，而事实
是根本没看过——这类假象比报错危险。此时界面上会明确写出能力不足。

---

## 安全须知

1. **本工具会写块**。对已挂载的卷做原始块写入必然破坏文件系统，代码里
   强制要求先锁卷，并对该路径默认拒绝。
2. 系统卷执行整理要求手输盘符确认，不提供"回车即通过"的路径。
3. 演示模式（`--demo` / 「演示模式」按钮）在内存里造一个 32 MiB 合成卷，
   全程不碰真实磁盘，用于验收与演示。
4. `verify`（回读校验）默认开启。关掉会快一倍，但坏块不会被发现。

---

## 目录速览

```
src/lbamover/{contract,engine,devices,platform,ui}/   见 docs/ARCHITECTURE.md
tests/                     214 个用例
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
