# v2 引擎层独立验证报告（QA）

| 项 | 内容 |
|---|---|
| 报告人 | QA 严过关 |
| 被验对象 | v2 引擎层 T01 安全底座 / T02 MFT 双通道 / T03 算法层 / T04 执行引擎 |
| 判定基准 | `docs/ARCH-DELTA-v2.md`（1891 行）INV-1~INV-8、§4.6.5、§7 验收条 |
| 工程师自报 | 「222 用例全绿 / MFT 抽样 32/32 一致 / IS_PASS: YES」 |
| **本报告结论** | **IS_PASS: NO —— 10 项问题，其中阻断 1 / 严重 4 / 一般 3 / 建议 2** |
| 路由决定 | **Engineer**（源码存在缺陷，详见 §7） |
| 取证方式 | 全部由本人独立编写脚本（`qa_work/`），**不复用工程师用例作为判据**；真盘只读（卷信息 / 位图 / 取回指针 / `$MFT` 解析），**全程未执行过一次 `FSCTL_MOVE_FILE`** |

---

## 0. 结论摘要

| 验证项 | 结论 | 一句话 |
|---|---|---|
| P0-1 介质检测常量 | **修复后基本通过，仍留 1 处严重缺陷** | 常量 4→7 已改对（交叉验证 7/7 双路线互证、9/9 与 `Get-PhysicalDisk` 一致）；但 `decode_seek_penalty` 无长度上界，28B/24B/全零缓冲仍会被判成"确定的 SSD" |
| P0-2 单一写出口 + 只读通道 | **通过** | 13 条 guard 拒绝条件全部 `MOVE=0`；导入隔离 8 例无污染；`mft_reader` 两处 `open_handle` 均为 `GENERIC_READ` |
| P0-3 MFT 解析正确性 | **失败（严重）** | 真盘 0 崩溃 / 0 越界、对抗样本 13/13 通过；但 **F: 86 个、H: 100 个文件**被解析成"可移动 + 0 簇 + 空 extent + 无原因"，规划层会当成"已就位" |
| P0-4 算法不变量 INV-1~8 | **失败（1 阻断 + 3 严重）** | 不变量本身全成立、环处理全通过；但执行期用陈旧位图（20 段里 10 段被拒）、relay 回退是死代码、`path_known` 被丢弃 |
| P0-5 v1 等价性 | **v1 侧通过；v2 侧声称被证伪** | v1 路径 6/6 逐字段一致；"v2 末端模式 == v1" **2 一致 / 4 不一致** |
| P1 各项 | **通过（2 条建议）** | `clamp` 下限不可绕过、批上限 12/12、journal 三次 fsync 可观测、`on_error` 两态正确、失败记录五要素齐全、降级留痕三处齐全 |

---

## 1. 环境与取证方式

- 机器：Windows，卷 `C: D: E: F: G: H: I: R: Z:`（`Z:` 网络盘，按指示排除；`R:` 亦为网络/虚拟卷，`IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS` 返回 `disk=0xFFFFFFFF`，仅作对照）
- 物理盘真值（独立数据源 `Get-PhysicalDisk`）：
  `2|HDD|TOSHIBA HDWD120`、`5|Unspecified|TOSHIBA External USB 3.0`、`4|SSD|WDC WDS250G2B0C`、`6|HDD|ACASIS EC-7351`、`0|HDD|ST8000NM017B`、`3|SSD|J.ZAO QL SERIES 1TB`、`7|Unspecified|Samsung Flash Drive`、`1|HDD|ST1000LM035`
- 权限实况：非管理员。`GENERIC_READ` 仅 `F:/H:/R:` 可开；`C:/D:/E:/G:/I:` 返回 `err=5`。`access=0` 可开全部卷设备（两个 IOCTL 均为 `FILE_ANY_ACCESS`）
- 本人脚本与原始输出（均在本仓库，可复跑）：

| 脚本 | 覆盖 | 证据文件 |
|---|---|---|
| `qa_work/p0_1_media_constant.py` | 原始缺陷取证：prop 0/3/4/6/7 逐项 | `p0_1_output.txt` |
| `qa_work/p0_1b_dump.py` | 描述符全量 hex（out_size 1024 vs 8） | `p0_1b_output.txt` |
| `qa_work/p0_1c_probe_media.py` | 调工程师的 `probe_media()` 对真值 | `p0_1c_output.txt` |
| `qa_work/p0_1d_schemes.py` | 5 种解码方案对真值打分 | `p0_1d_output.txt` |
| **`qa_work/p0_1e_crosscheck.py`** | **交叉验证：裸 ctypes + 物理盘句柄 + 换 QueryType** | `p0_1e_output.txt` |
| **`qa_work/p0_1f_verify_fix.py`** | **修复复验（含对抗样本）** | `p0_1f_output.txt` |
| `qa_work/p0_2_isolation.py` | 写出口收敛 / 只读隔离 / guard 拒绝矩阵 | `p0_2_output.txt`、`p0_2_isolation_r2.txt` |
| `qa_work/p0_3a_realdisk.py` / `p0_3c_precise.py` | 真盘 MFT vs 取回指针（三元组） | `p0_3a_output.txt`、`p0_3c_F.txt`、`p0_3c_H.txt` |
| `qa_work/p0_3b_adversarial.py` | 手搓 NTFS 记录篡改 B1–B13 | `p0_3b_output.txt` |
| `qa_work/p0_3d_record_dump.py` | 问题记录的属性布局取证 | `p0_3d_F.txt` |
| **`qa_work/p0_3e_reclassify.py`** | **复核"真实差异"分类（剔除我自己的稀疏误判）** | `p0_3e_output.txt` |
| `qa_work/p0_4_invariants.py` / `p0_4b_deep.py` | INV-1~8、12 组对抗场景、环处理 | `p0_4_output.txt`、`p0_4b_output.txt`、`p0_4_invariants_r2.txt` |
| `qa_work/p0_5_p1.py` | v1 基线 / P1 全项 | `p0_5_p1_output.txt`、`p0_5_p1_r2.txt` |
| `qa_work/p0_6_audit_tests.py` | 工程师用例 AST 审计 | `p0_6_output.txt` |

---

## 2. P0-1 介质检测常量

### 2.1 最终答案

> **正确答案是 `7`（`StorageDeviceSeekPenaltyProperty = 7`）。工程师原实现写的 `4` 是 `StorageDeviceWriteCacheProperty`，是错的。**
> 并且**只把常量改成 7 仍然不够** —— 本机驱动返回的描述符是 **12 字节**，布尔在 **offset 8**，而不是 SDK 头文件写的 8 字节 / offset 4。

证据（`qa_work/p0_1e_crosscheck.py`，裸 ctypes，不经 `app.win32.api`）：

```
[0] 传参校准：prop=0 → C: len=396  Version=40  Size=396        (期望 Version=40 ✓)
    同一查询申请 8 字节缓冲：ok=True len=8   ← 驱动截断，"Size 看起来吻合" 是假象

[1] prop=7 两种句柄路线（卷句柄 / 物理盘句柄）
  C:  disk3  卷 12 0c...0c...00 | 物理盘 12 0c...0c...00
  D:  disk2  卷 12 0c...0c...01 | 物理盘 12 0c...0c...01
  E:  disk1  卷 12 0c...0c...01 | 物理盘 12 0c...0c...01
  F:  disk6  卷 12 0c...0c...01 | 物理盘 12 0c...0c...01
  G:  disk4  卷 12 0c...0c...00 | 物理盘 12 0c...0c...00
  H:  disk5  卷 err=1          | 物理盘 err=1     ← 不支持，不是解码失败
  I:  disk0  卷 12 0c...0c...01 | 物理盘 12 0c...0c...01
  [2] 一致性判定：7/7 一致  ← 两条独立路线互证

[3] offset4 vs offset8
  C/D/E/F/G/I/R：raw[4] 恒为 12（= Size），raw[8] 才是 0/1
  → offset4 = 12 恒非零 → 任何把 offset4 当布尔的写法"恒判 HDD"

[5] prop=4 全量 28 字节：1c 00 00 00 1c 00 00 00 02 02 02 01 01
[5] prop=6 全量 28 字节：1c 00 00 00 1c 00 00 00 00 00 00 00 00 02 00 00 00 10 00 00 00
    dwords=[28, 28, 0, 0, 512, 4096, 0]  → offset16=512(逻辑扇区) offset20=4096(物理扇区)
    → 证实 prop=6 是 STORAGE_ACCESS_ALIGNMENT_DESCRIPTOR（不是 3）
```

### 2.2 与主理人实测结论的交叉验证结果

**完全一致，无需修正主理人结论。** 具体：

| 主理人结论 | 我的独立复现 | 是否吻合 |
|---|---|---|
| prop=7 返回 12 字节，`IncursSeekPenalty` 在 offset 8 | 卷句柄与**物理盘句柄**两条路线均为 len=12、flag@8 | ✅ |
| `offset4=28` → `bool()` 恒真 | 实测 `raw[4]` 在 prop=7 下是 **12**（`Size`），在 prop=4 下是 28 —— 恒非零，结论成立 | ✅ |
| H: `err=1` 属于"驱动不支持" | H: 在**卷句柄和物理盘句柄上都是 err=1（ERROR_INVALID_FUNCTION）**；同一块盘（disk5 TOSHIBA External USB 3.0）无一路径可查 | ✅ |
| 传参校准 prop=0 → Version=40 | C: len=396 / Version=40 / Size=396 | ✅ |
| 6/6 与 `Get-PhysicalDisk` 一致 | 修复后 **9/9** 一致（含 3 个 unknown 卷） | ✅ |

**一处需要更正主理人对"原缺陷的失效机制"的描述**（不影响判定，但影响修复方向）：

> 主理人写"`offset4=28`，`bool(28)` 恒真 → 全部误判为 HDD"。

原实现（`prop=4` + `out_size=8` + `if version != 1: return None` + `return raw[4] != 0`）在真盘上的实际行为是：
`raw = 1c 00 00 00 1c 00 00 00` → `version = 28 ≠ 1` → **`return None`** → 降级到 BusType → `MEDIA_UNKNOWN`。
即**当时 6/6 全部落到 `unknown`（保守模式）**，不是"全部 HDD"。我的 `p0_1c_output.txt` 实测正是 6/6 `medium=unknown`。

这个区别重要：如果工程师只删掉 `version != 1` 那道闸，`raw[4]=28` 就会真的变成"全部判 HDD"。**两种失效模式都让 N3 死掉，但"静默降级为 unknown"比"静默错判 HDD"后果轻**（前者只是没加速，后者会让 SSD 走 HDD 的预热/批大小策略）。修复必须同时动常量和布局，不能只动一处 —— 工程师这次做对了。

### 2.3 修复复验（`qa_work/p0_1f_verify_fix.py`）

| 验收条（主理人给出） | 结论 | 证据 |
|---|---|---|
| 常量 == 7 且显式 `!= 4` / `!= 6` | **PASS** | `app/win32/storage.py:38`；`tests/test_storage.py:79-81` |
| `len=12` 读 offset 8 | **PASS** | `decode_seek_penalty(0c..0c..01)==True`，`0c..0c..00==False` |
| `len=8` 回退 offset 4 | **PASS** | `01..01..==True`，`01..00..==False` |
| 其它长度判 unknown | **FAIL（4 例）** | 见下 |
| `err=1` 判 unknown 而非 False | **PASS** | H:/K:/L: 均 `media=unknown, source=bus_type`，未落 False |
| 回归夹具钉住 `len=28 + offset4=0x1c` → 必须 unknown | **不存在该夹具，且当前实现会判错** | `tests/test_storage.py` 无 28B 用例 |
| 无残留 `bool(raw[4])` / `version != 1` / `_SEEK_DESC_SIZE` | **PASS** | 全仓文本扫描 0 命中 |
| 真实卷 6/6 一致 | **PASS（9/9）** | 见下 |

复验失败明细（`p0_1f_output.txt`）：

```
[V3] 非 SeekPenalty 描述符必须判 unknown
  PASS  prop=4 真实字节(IsWriteCacheEnabled=2)        → None
  FAIL  prop=6 真实字节(AccessAlignment)              → False   ← 判成 SSD
  FAIL  写缓存描述符但 IsWriteCacheEnabled=0          → False   ← 判成 SSD
  FAIL  未知 24 字节描述符                            → False   ← 判成 SSD
  FAIL  全零 12 字节（驱动没填）                      → False   ← 判成 SSD

[V5] 全卷探测 vs Get-PhysicalDisk 真值
  C: ssd/ssd Y   G: ssd/ssd Y   D: hdd/hdd Y   E: hdd/hdd Y
  F: hdd/hdd Y   I: hdd/hdd Y   H/K/L: unknown/unknown Y   → 9/9
```

**根因**（`app/win32/storage.py:180-186`）：

```python
if _SEEK_DESC_COMPACT <= size <= len(raw) and size < _SEEK_DESC_WITH_HEADER:   # size ∈ [8,11]
    offset = 4
elif size >= _SEEK_DESC_WITH_HEADER and len(raw) >= _SEEK_DESC_WITH_HEADER:    # ← 无上界
    offset = 8
else:
    offset = 8 if len(raw) >= 12 else 4
```

`size >= 12` 没有上限，任何 12 字节以上的描述符都会走到 `offset=8`。于是：

- 常量若被改回 `6` → 28 字节对齐描述符 `raw[8]=0` → **判 SSD**
- 常量若被改回 `4` 且写缓存被关掉 → `raw[8]=0` → **判 SSD**
- 驱动返回全零缓冲（设备未就绪 / USB 桥接挂起）→ **判 SSD**

今天之所以没炸，是因为本机 prop=4 的第 8 字节恰好是 `2`（`IsWriteCacheEnabled=2`），落进了 `not in (0,1)` 的保护。**这是靠一个偶然字节值兜住的，不是靠逻辑。**

**修复建议**（一行）：把上界卡死为真实存在的两种布局 ——
`if size == 8 → offset 4 elif size == 12 → offset 8 else return None`；`Size` 字段为 0 时按 `len(raw)` 兜底也只在 `len(raw) in (8, 12)` 时兜底。

**另一处需要改的夹具**（`tests/test_storage.py:113-114`）：

```python
# 全零（驱动没填）也不能被当成"确定是 SSD"
assert storage.decode_seek_penalty(bytes(12)) is False
```

注释说"不能被当成确定是 SSD"，断言却要求它**必须** `is False`（即就是 SSD）。注释与断言相反，且断言钉住的是错误行为。应改为 `is None`。

### 2.4 对 N3（HDD 加速）的影响

- **修复前**：6/6 卷 `MEDIA_UNKNOWN` → `io_profile.media_policy("unknown")` → `prewarm=False` → 预热永不触发，批大小走保守值。**静默失效，无数据风险**，UI 长期显示"未知介质 · 保守模式"。
- **修复后**：6/6 正确。HDD（D/E/F/I）会真的进入 `prewarm=True` + 大批（`MAX_BATCH_CLUSTERS=65536`）路径。
- **残留风险**：若修复的 4 个洞被触发（尤其是全零缓冲），会**把 HDD 判成 SSD**，反而关掉 HDD 加速 —— 与修复前的后果同类，因此这条必须修完才算 P0-1 关闭。

---

## 3. P0-2 单一写出口 + 只读通道 —— 通过

脚本：`qa_work/p0_2_isolation.py` → `p0_2_isolation_r2.txt`（Round 2 复跑，全 PASS）

| 检查 | 结论 | 证据 |
|---|---|---|
| `FSCTL_MOVE_FILE` 仅出现在 `app/win32/mover.py` | PASS | 全仓 AST 扫描，其它模块 0 引用 |
| MFT 通道 0 个 `GENERIC_WRITE` / `WriteFile` | PASS | `core/mft.py`、`win32/mft_reader.py` |
| `mft_reader` 的 `open_handle` 权限 | PASS | 2 处调用均为 `api.GENERIC_READ`（AST 取实参，不靠 grep） |
| `import app.core.mft` / `import app.win32.mft_reader` 后 `'app.win32.mover' in sys.modules` | PASS | 8 个子进程隔离用例全部 `CLEAN` |
| `mover` 是否被间接导入 | **PASS（但有一处需登记）** | `scanner.py:879` `class MoveWorker > run` 内的**函数内导入**，导入 `scanner` 阶段不会加载 `mover`；这是刻意设计（执行期才拿写出口），不构成隔离缺陷 |
| guard 拒绝 → MOVE 调用次数 | PASS 13/13 | 卷首/卷尾/MFT 区/`$MFT` 守卫窗/`$MFTMirr` 守卫窗/目标重叠/缺位图/位图非空闲/负 LCN/零簇/越卷尾/… 全部 `allow=False, move_calls=0, failures=1` |
| 无 guard 且无 `validate_target` → 拒绝 | PASS | `move_calls=0` |
| `execute_task` 路径未知 → 拒绝 | PASS | `move_calls=0` |
| **观测力自检**（guard 放行时 MOVE 恰好 1 次） | PASS | `move_calls=1` —— 这条保证上面所有 `0` 不是"monkeypatch 没装好" |

> 说明：这一项我特意加了正向对照。否则"13 条全 0"也可能只是因为我的桩没挂上。

---

## 4. P0-3 MFT 解析正确性 —— 真盘解析无越界，但产出了一类"空壳可移动文件"

### 4.1 真盘规模与稳定性

| 卷 | 文件条目 | bad_magic | bad_fixup | bad_attr | bad_runlist |
|---|---|---|---|---|---|
| F: (ACASIS USB HDD, 1.4 TB) | 202,999 | 0 | 0 | 0 | 0 |
| H: (TOSHIBA USB) | 70,280 | 0 | 0 | 0 | 0 |

### 4.2 与 `FSCTL_GET_RETRIEVAL_POINTERS` 的三元组比对

基线是**我自己写的** `RETRIEVAL_POINTERS_BUFFER` 解码器（`ExtentCount@0` / `StartingVcn@8` / 每项 16 字节 `NextVcn@0, Lcn@8`），不引用工程师任何代码。比对单位是 `(vcn, lcn, clusters)` 三元组，不是字节数。

`p0_3c_F.txt`：

```
F: 原始逐三元组相等 38/60，仅尾部 sparse 差异 0，真实差异 6，无法验证 6
H: 原始逐三元组相等 32/45，真实差异 6，无法验证 7
```

**先做一次自我复核（这一条必须写清，否则会误伤实现）**：`p0_3a_realdisk.py` 的初版输出里另有 3 例"不一致"（H: `zadig-2.9.exe`、`马督工-江苏退休金问题 (1)/(2).m4s`），经 `qa_work/p0_3e_reclassify.py` 复核，**这 3 例是我自己的比对方漏了"全稀疏"情形，不是缺陷**：

```
H:\fdmdownload\zadig-2.9.exe
   size=5334088  clusters=0  fragments=0  movable=False
   reason='稀疏文件（仅已分配部分可搬，v2 暂不支持）；含 ADS，未计入'
   triples=[[0, -1, 1312]]        ← LCN=-1 是 sparse，MFT 侧正确
```

对全稀疏文件，`$MFT` 里本就没有已分配 extent，句柄侧返回 `[(vcn, -1, clusters)]`，两侧本应视为一致。工程师的实现**在这点上是对的**：

```
compare_extents(MFT 全稀疏, 句柄全稀疏) = True
compare_extents(MFT 全稀疏, 句柄空)     = True
compare_extents(MFT 空,     句柄全稀疏) = True
_extent_triples([Extent(lcn=-1)])      = []     ← 尾部稀疏被正确剔除
```

且 `record_to_entry` 对稀疏文件的处置**值得肯定**：`movable=False`、`clusters=0`、reason 明确写"仅已分配部分可搬，v2 暂不支持"，没有把它当成"0 簇可移动"混进规划。另外 `sample_pick` 的候选池要求 `allocated_clusters > 0`，全稀疏文件天然不进抽样，所以不会造成 `FALLBACK_SAMPLE_MISMATCH` 误降级。

（我初版 `p0_3a` 里那行 `compare_extents(entry.extents, [])` 也是错的 —— 拿空列表当句柄侧比，结果无意义，已废弃。）

排除了稀疏这一类之后，剩下的 6 个"真实差异"**全部**是同一形态，例如：

```
✗ F:\Program Files\qBittorrent\qbittorrent.pdb
  size=0 movable=True reason=''
  MFT : []
  句柄: [(0, 5215997, 15256), (15256, 5231253, 8085), (23341, 5239338, 9482)]
✗ F:\Program Files (x86)\WeGame\TLog\20240110_081717.log
  size=0 movable=True reason=''
  MFT : []
  句柄: [(0, 27129106, 4736), (4736, 27133842, 771)]
```

### 4.3 根因（这是 P0-3 的唯一实质缺陷）

`p0_3d_record_dump.py` 取原始记录：

```
record 11779 (qbittorrent.exe) / record 7764 (BUENA-213.mp4):
  has_attribute_list = True
  基础记录内 $DATA 属性：不存在（在扩展记录里）
```

`core/mft.py::parse_record` 的完整性判断：

```python
covered = 0 * bytes_per_cluster = 0
size    = 0
size > covered  → False       # 于是 incomplete = False
```

`record_to_entry` 随后返回 `movable=True, size=0, extents=[], reason=''`。

规模统计（判据 `movable=True and size==0 and clusters==0 and record_no>15`，即规划层会当作"0 簇、无需搬动"的条目）：

| 卷 | 条目数 | 其中 `reason` 为空（静默） | `reason` 非空（如"含 ADS，未计入"） |
|---|---|---|---|
| F: | **86** | 68 | 18 |
| H: | **100** | 100 | 0 |

两列都是缺陷：`reason` 为空的那批会**静默**进入"已就位"；`reason` 非空的那批虽然留了一句话，但 `movable` 仍为 `True`、`clusters` 仍为 0，规划层照样不会搬它。

### 4.4 为什么这是"严重"而不是"一般"

规划层看到的是一条 **0 簇 + 空 extent + 可移动 + 无阻断原因** 的条目：

1. `packer`/`tail` 侧会判"需要 0 簇"，**任何位置都放得下**，于是标成已就位 / 无需移动；
2. UI 显示"已整理"，但这个文件的真实碎片（上例 3 段、共 32,823 簇 ≈ 128 MB）**一个字节都没动**；
3. 用户据此认为整理成功 → **假成功**，与"数据损坏"同类的信任破坏，只是不丢数据。

### 4.5 为什么 §4.6.5 的句柄兜底没救到它

`win32/mft_reader.py:566-585`：

```python
if "属性列表" not in entry.reason:
    continue
```

这些条目的 `reason` 是**空字符串**（不是"属性列表…"），所以兜底分支直接跳过。修法二选一：

- 在 `parse_record` 里判 `has_attribute_list and not data_attr_found` → `incomplete = True`、`reason = "属性列表跨记录，$DATA 在扩展记录"`；
- 或把兜底触发条件从"`reason` 含属性列表"改成"`extents` 为空且 `size` 为 0 但记录 `in_use`"。

**可复现步骤**：

```bash
cd lba-mover
python qa_work/p0_3d_record_dump.py F 11779     # 看基础记录无 $DATA
python qa_work/p0_3c_precise.py scan  F        # ~72s，缓存到 cache_F.json
python qa_work/p0_3c_precise.py analyze F      # 打印上述 6 个差异
```

### 4.6 对抗构造（全部通过）

`p0_3b_adversarial.py`，手搓 FILE 记录 + 篡改，13 组：

| 编号 | 构造 | 结论 |
|---|---|---|
| B1 | magic 改为 `XXXX` | `bad_magic=1`，不崩 |
| B2 | 全零记录 | `in_use=False`，`bad_magic=0`（正确判"未使用"） |
| B3 | `usa_count` 改错 | `bad_fixup=1` |
| B4 | 尾扇区撕裂 | `bad_fixup=1`，不崩 |
| B5 | runlist 负偏移 | 无负 LCN extent，产出 warning |
| B6 | runlist 截断 | 检出 |
| B7 | 偏移字段长度 0 | 检出并 warning |
| B8 | LCN = `0xFFFFFFFF` | 诚实解析，随后被 `check_physical` 拦下 |
| B9 | LCN = 1e9（越界） | 解析后拦住，不越界读 |
| B10 | run 数超 `MAX_RUNS_PER_FILE` | 截断并计入 |
| B11 | 记录 0–15 | 全部排除；16 接受；`$Root`(5) 排除但路径可重建 |
| B12 | 常驻 `$DATA` 小文件 | `movable=False`，reason 含"常驻"，`allocated_clusters=0`（不是"0 簇可移动"） |
| B13 | 有 ADS | 主流排除，`ads_skipped=1` |
| B14 | `$DATA` vcn 有洞（0, 8） | `incomplete=True`，`"属性列表跨记录，布局未知"` |
| B15 | 4Kn fixup / 512 误读 | 4Kn 正常；512 误读报 `bad_fixup` 不崩 |

**扇区对齐越界**（`MftSpanGuard.check`）：

```
normalize(100, 100) == (0, 512)
check(0, 1000) with volume_bytes=1000  →  抛 MftBoundsError（对齐后 1024 > 1000）
                                       （未对齐的朴素判断会放行 → 证明对齐检查真的生效）
```

`iter_records` 越界：在真盘 `\\.\F:` 上 yield 第 1 条（`FILE` magic 已核）后抛 `MftBoundsError`。

---

## 5. P0-4 算法不变量 —— 不变量成立，但执行期有三处实质缺陷

脚本：`qa_work/p0_4_invariants.py`、`p0_4b_deep.py` → `p0_4_invariants_r2.txt`

### 5.1 不变量本身：全部成立

对抗场景 S1–S12（1 簇区间 / 区间 == 整卷 / 区间 == 0 / 0 文件 / 单文件远大于区间 / 文件数远超容量 / 跨 MFT 禁区 / 跨首尾保留 / 自重叠 / 环）：

| 不变量 | 结论 |
|---|---|
| INV-1 目标区间互不重叠 | PASS |
| INV-2 目标 ⊆ 允许区 | PASS |
| INV-3 目标连续且长度精确 | PASS |
| INV-4 目标 ∩ 禁区 = ∅ | PASS |
| INV-5 执行时刻位图空闲 | **FAIL（见 Bug A）** |
| INV-6 不得自 src 重叠（除非 relay） | PASS |
| INV-7 不得"目标覆盖后续任务的源" | PASS |
| INV-8 被驱逐目标安全 | PASS |
| 段内重叠（同一 item 内交叉） | PASS |

依赖序正确性（"先写后读"逆序 → 数据被覆盖）**逐场景检查过**：S1/S2/S4/S8/S9 中，对每条依赖边 `(u→v)` 验证 `v.src` 区间与 `u.dst` 区间不相交，或用 relay 中转；未发现"写覆盖待读源"的排序错误。这是最关键的一项，结论是**干净**。

环处理全通过：

```
2 环：edges {0:{1},1:{0}} → Kahn 余 [0,1]，Tarjan [[0,1]]
     break_cycles rounds=1 ≤ 3，split 后 3 个任务，无依赖违例
3 环 SCC [[0,1,2]] → rounds=1
自环 [[0]]
6 个二元环 → 触及 3 轮上限，明确报错"依赖环过多（>3 层中转），建议改小目标区间或分批执行"（不死循环）
```

### 5.2 Bug A（**阻断**）：`MoveWorker` 用规划期的陈旧位图建 `SafetyGuard`

`app/core/scanner.py:877-1010`：`SafetyGuard(policy, self._snapshot)` 在循环外**只创建一次**，用的是**规划时刻**的位图快照。而执行过程中每次成功搬运都会改变位图（源变空闲、目标被占用），再加上驱逐任务会先腾出空间 —— 静态快照必然过期。

量化（`p0_4b_deep.py`，S9-strong：40 簇窗口被占满、10 个被选文件各 4 簇、10 个 blocker，plan 产出 20 任务 / 20 段）：

```
静态快照（= 当前实现）  guard 拒绝 10/20 段
每步刷新位图            guard 拒绝  0/20 段
```

被拒绝的段在 `execute_segments` 里被**静默跳过**（记一条 failure 后继续）。

后果：**所有依赖驱逐的计划在运行期都会失败**，即 v2 的核心能力（"目标区间被占用时先腾走"）实际上不工作。这直接违反 INV-5（"目标块在执行时刻位图空闲"），也是唯一能解释"规划看起来没问题但搬不动"的机制。**列为阻断项。**

**修复方向**：`MoveWorker` 在每段执行前用实时位图重建/刷新 guard（或让 guard 在 `reserve()`/`release()` 时维护自己的活位图），而不是复用 `self._snapshot`。

**可复现**：

```bash
python qa_work/p0_4b_deep.py     # 看 "静态快照拒绝 10 / 动态刷新拒绝 0"
```

### 5.3 Bug B（**严重**）：`packer._place_one` 的 relay 回退是死代码

`_place_one` 里声明了 `fallback` 变量并按注释应在"任何落点都自重叠"时返回段首让上层标 relay，但实际候选序列 `[run_start, widest[1], run_end - need]` 中，`run_end - need` **永远落在该 run 内且永远 take 成功**，于是 `fallback` 分支不可达。

实测：

```python
_place_one(entry2 extents=[(100,5),(110,5)], ledger=[(100,115)],
           allowed=[(100,115)], avoid_self_overlap=True)
→ None        # 期望 (100, 5) 段首，让上层标 relay
```

后果：有 15 簇连续空间、但任何落点都自重叠的场景，被报成 `impossible`（"即使腾走全部可驱逐簇，仍差 10 簇"），而不是 `role="relay"`。连带 `place_with_relay()` 与 `build_range_plan` 的 relay 分支**从打包路径不可达**，只剩 `break_cycles` 能产生 relay。

设计文档 §459 明确写了"`_place_one` 的 fallback 应让上层标 relay"，实现与设计不符。

### 5.4 Bug C（**严重**）：`MoveTask.path_known` 在任务重组时被丢弃

```python
# scheduler.py:495-503  merge_task_segments_for_execution
MoveTask(...)          # 未传 path_known= → 默认 True
# scheduler.py:370-383  split_task_for_relay
MoveTask(...)          # 同样未传
```

实测：

```
merge_task_segments_for_execution 后 path_known = True   （应保留 False）
split_task_for_relay              后 path_known = [True, True]（原为 False）
```

`execute_task` 的 `path_known` 防线因此形同虚设，只剩 `path.startswith("(record ")` 这条字符串兜底。若将来路径生成方式改变（例如改成 `(unknown)` 前缀或空串），这道防线会静默失效。

### 5.5 次要：`plan.feasibility` 可能为 `None`

`window.clusters<=0` / `not selection` / `total<=0` 三条早返回路径不填 `feasibility`，而 `MovePlan.feasibility` 类型是 `Optional[...]`，UI 侧 `app/ui/main_window.py:1378` 已有 `is not None` 守卫，故**只登记为 API 形状不一致（建议级）**，不算缺陷。

---

## 6. P0-5 v1 等价性 —— v1 侧通过，v2 侧声称被证伪

脚本：`qa_work/p0_5_p1.py` → `p0_5_p1_r2.txt`

### 6.1 v1 路径：真的没变

我用**自己独立推导的 v1 基线**（不读 `tests/test_planner_compat.py` 的快照）比对 6 组场景：

| 场景 | 结论 |
|---|---|
| 空集 | 逐字段一致 |
| 单文件 | 一致 |
| 已就位 | 一致 |
| 超出预留 | 一致 |
| 含 MFT 洞 | 一致 |
| 多碎片 | 一致 |

`PlanParams` 的 4 个新增字段全为 0 时，`build_plan` 输出与 v1 **完全相同**。**这一条通过。**

### 6.2 "v2 末端模式 == v1"：2 一致 / 4 不一致

| 场景 | v1 | v2 末端模式 |
|---|---|---|
| 单文件 | `('a.bin', 4092, 4)` | `('a.bin', 3584, 4)` |
| 已就位 | `3600` | `3584` |
| 含 MFT 洞 | `[('a.bin',4084,4),('b.bin',4088,8)]` | `[('b.bin',3072,8),('a.bin',3080,4)]` |
| 多碎片 | `[('c.bin',2035,1),('b.bin',2036,6),('a.bin',2042,6)]` | `[('a.bin',1792,6),('b.bin',1798,6),('c.bin',1804,1)]` |

这**不是**数据损坏：设计 R12 允许 v2 在末端留 1 MiB 守卫（`tail_guard_clusters`，置 0 可复现 v1）。问题是：

- `ARCH-DELTA-v2.md:1459` 写的是"`build_plan` 输出逐字段等价 v1" —— 这句只对 `build_plan`（v1 路径）成立；
- 若有人把"v2 末端模式 == v1"当作回归基线（v1→v2 迁移的验收依据），会得到 4/6 不一致，从而误判"迁移失败"或反过来"顺手把守卫删掉"。

**处理建议**：把该条验收的措辞改为"`build_plan`（v1 路径）逐字段等价"，并显式文档化"v2 末端模式与 v1 的差异 = 尾部 1 MiB 守卫，`tail_guard_clusters=0` 可复现 v1 数值"。**代码不用改，口径必须改。**

---

## 7. P1 各项

| 项 | 结论 | 证据 |
|---|---|---|
| `clamp_reserve_mb` 下限不可绕过 | **PASS** | bpc ∈ {512, 4096, 65536} × total ∈ {1e5, 2e7}，request=0 与 request=-100 均 ≥ 下限（2 MB → 4096/512/32 簇 vs 下限 2048/256/16） |
| 簇上限 65536 与"实际生效值"上报 | **PASS** | 12 组合全部落在 [16, 65536]；8 GiB@4K → 恰好 65536；`capped=True`；`batch_bytes=268435456`；`effective_mb=256 == batch_bytes//MB`；`cap_note='实际生效 256 MB（单次搬运上限 65,536 簇）'`；64 MiB 不被 cap |
| `journal` 逐段 flush + fsync（**可观测**） | **PASS** | 写 1 条后**立刻**用第二个句柄读到 1 行（证明 flush）；3 条 → 3 行；`fsync` 被调用 **恰好 3 次**（mock 计数） |
| 回滚：逆序 + `src↔dst` 互换 | **PASS** | 段序 `[('b.bin',200,2),('a.bin',100,1)]` = 逆序 + 互换；失败段被排除；尾部截断行跳过不抛 |
| `on_error` `continue` / `halt` | **PASS** | monkeypatch 执行器（零写盘）：continue→执行 3 个任务，halt→1 个 |
| `Progress` 终值 == `plan.total_move_clusters` | **PASS** | 两态分母均 12 == `plan.total_move_clusters` 12 |
| 失败记录五要素 | **PASS** | `path / src_lcn=10 / dst_lcn=100 / clusters=4 / winerror=5` 齐全；`moved_clusters=0, moved_files=0`（不虚报成功） |
| 自动降级留痕三处 | **PASS** | K:(exFAT) 与 C:(无权限) 均 `fallback_code` 非空 + `fatal_error` 非空 + `notes` 非空 + `channel='handle'` |

两处建议：

1. **`clamp_reserve_mb` 的 `+1` 过冲**：请求恰好等于下限时被放大到 2 MB（1954 MB vs 目标 1953.125 MB）。对 `total//2` 上限的超出量 ≤ `MB//4096` 簇，安全但数值不直观，建议改 `ceil_div` 而非 `// + 1`。
2. **降级原因码不一致**：同一个 exFAT 卷，`scan_volume_mft` 报 `fallback_code='mft_runlist_unavailable'`（文案"卷不是 NTFS，无法直读 $MFT"），而 `scanner.build_scan_channel()` 对同一情形返回 `FALLBACK_NOT_NTFS`。UI 若按码做分支会漏掉一半。建议统一。

---

## 8. 工程师用例审计（AST 级）

脚本：`qa_work/p0_6_audit_tests.py` → `p0_6_output.txt`（对辅助函数下钻，避免误判）

| 文件 | 用例 | 断言 | 密度 | 真恒真 | 吞断言 | 真空壳 |
|---|---|---|---|---|---|---|
| test_executor.py | 12 | 43 | 3.6 | 0 | 0 | 0 |
| test_guard.py | 16 | 33 | 2.1 | 0 | 0 | 0 |
| test_io_profile.py | 21 | 66 | 3.1 | 0 | 0 | 0 |
| test_journal.py | 11 | 35 | 3.2 | 0 | 0 | 0 |
| test_mft_channel.py | 24 | 100 | 4.2 | 0 | 0 | 0 |
| test_mft_fixup.py | 31 | 106 | 3.4 | 0 | 0 | 0 |
| test_mft_runlist.py | 10 | 19 | 1.9 | 0 | 0 | 0 |
| test_ownership.py | 8 | 20 | 2.5 | 0 | 0 | 0 |
| test_packer.py | 29 | 104 | 3.6 | 0 | 0 | 0 |
| test_planner_compat.py | 9 | 41 | 4.6 | 0 | 0 | 0 |
| test_safety.py | 29 | 65 | 2.2 | 0 | 0 | 0¹ |
| test_scheduler.py | 19 | 46 | 2.4 | 0 | 0 | 0 |
| test_storage.py | 9 | 34 | 3.8 | **1** | 0 | 0 |
| **合计** | **228** | **712** | **3.1** | **1** | **0** | **0** |

¹ `test_assert_accepts_valid_targets` 是 "must-not-raise" 型（调 `assert_target_allowed` 期望不抛），非空壳。

**结论：228 个用例没有空壳、没有 `assert True`、没有被 `try/except` 吞掉的断言，断言密度 3.1/用例 —— 是真在断言。** 全量复跑：**228 passed / 0 failed**（与工程师自报一致）。

那么"228 全绿"为什么没拦住介质误判？三条具体原因：

1. **唯一能拦住它的用例被一句恒真断言废掉了**
   `tests/test_storage.py:156`：`assert probed >= 0`（计数恒真，注释自己都写了"若真的一个都没有…也不算失败"）。原实现下 `probed == 0`，这条断言照样通过 —— 这正是"222 全绿 + 6/6 误判"能共存的原因。
2. **其余介质用例只验"不崩 / 枚举合法"**
   `assert probe.media in (MEDIA_HDD, MEDIA_SSD, MEDIA_UNKNOWN)` —— `unknown` 永远是合法值，介质全错也通过。缺的是"对独立真值源（`Get-PhysicalDisk`）比对"。
3. **"v2 与 v1 等价"零覆盖**
   `test_planner_compat.py` 全篇只调 `build_plan`（v1 路径），从未调用 `build_range_plan`。所以 §6.2 的 4/6 不一致它结构上就发现不了。

---

## 9. 问题清单（按严重度分级）

| # | 级别 | 位置 | 问题 | 复现 |
|---|---|---|---|---|
| 1 | **阻断** | `app/core/scanner.py:877-1010` | `MoveWorker` 用规划期陈旧位图建一次 `SafetyGuard`，执行期不刷新 → 依赖驱逐的计划 20 段里 10 段被静默拒，INV-5 违反 | `python qa_work/p0_4b_deep.py` |
| 2 | **严重** | `app/core/mft.py::parse_record` + `record_to_entry` | `has_attribute_list` 且基础记录无 `$DATA` 时产出 `movable=True, size=0, extents=[], reason=''`；F: 86 个 / H: 100 个；`mft_reader.py:566` 的兜底因 `reason` 为空而跳过 → "已整理"假成功 | `python qa_work/p0_3d_record_dump.py F 11779` |
| 3 | **严重** | `app/win32/storage.py:180-186` | `decode_seek_penalty` 无长度上界，28B/24B/全零缓冲被判成"确定的 SSD"（4 例失败）；今天靠 `IsWriteCacheEnabled=2` 这个偶然值兜住 | `python qa_work/p0_1f_verify_fix.py` |
| 4 | **严重** | `app/core/packer.py::_place_one` | relay 回退是死代码，`fallback` 不可达；自重叠场景返回 `None` → 报 `impossible` 而非 `role="relay"`；`place_with_relay` 从打包路径不可达 | 同上 `p0_4_invariants.py` 的 `_place_one` 用例 |
| 5 | **严重** | `app/core/scheduler.py:495-503`、`370-383` | `merge_task_segments_for_execution` / `split_task_for_relay` 重建 `MoveTask` 时丢 `path_known` → `execute_task` 的该防线失效 | `p0_4_invariants.py` S12 |
| 6 | 一般 | `docs/ARCH-DELTA-v2.md:1459` | "`build_plan` 输出逐字段等价 v1"被引用成"v2 末端模式 == v1"；实测 4/6 不一致（1 MiB 守卫，设计允许） | `p0_5_p1.py` |
| 7 | 一般 | `tests/test_storage.py:113-114` | 注释说全零"不能当成确定是 SSD"，断言却要求 `is False`（就是 SSD）；钉住了错误行为 | 同上 |
| 8 | 一般 | `app/core/mft.py` vs `app/core/scanner.py:66-96` | 同一 exFAT 卷降级原因码不一致（`mft_runlist_unavailable` vs `FALLBACK_NOT_NTFS`） | `p0_5_p1.py` P1-6 |
| 9 | 建议 | `app/core/safety.py::clamp_reserve_mb` | `clusters * bpc // MB + 1` 的 `+1` 使"请求恰好等于下限"被放大到 2 MB | `p0_5_p1.py` P1-1 |
| 10 | 建议 | `app/core/planner.py` 早返回路径 | `plan.feasibility` 为 `None`，而 `MovePlan.feasibility` 类型为 `Optional`（UI 已守卫，仅形状不一致） | `p0_5_p1.py` |

**合计：阻断 1 / 严重 4 / 一般 3 / 建议 2 = 10 项。**

---

## 10. 路由决定

> **Routing Decision: Engineer**
> 依据：第 1、2、4、5 项是**源码缺陷**（我的断言与设计文档/PRD 一致，实现给出的结果错误），不是我的测试写错。第 3 项同样是源码缺陷（修复不完整）。
> 第 6、7、8 项涉及文档口径与夹具，需工程师同步改。
> 我自己的测试在过程中被推翻过 10 次（`CreateFileW` 未设 `restype` 导致句柄截断、AST 下钻漏辅助函数误报 15 个空壳、`indexOfSignal(...) >= 0` 误判恒真、`src_ranges` 语义读错、vcn 洞构造错、`clamp` 的 `+1` 断言过紧、全稀疏文件比对漏判、`compare_extents(x, [])` 拿空列表当句柄侧、正则 `int()` 解析三元组失败等），均已修正，修正后的结论已在上文体现；**未因测试 bug 误报任何一条源码缺陷**。

## 11. 能否放行到 UI 集成阶段

**不能。** 理由：

- 第 1 项使 v2 的核心能力（目标区间被占用 → 先驱逐再搬运）在运行期全线失效，且失效方式是"规划成功、执行静默跳过"，UI 集成后表现为"点了没反应 / 进度条不动"，排查成本极高；
- 第 2 项会产生"已整理"的假成功，属于信任级缺陷；
- 第 3 项若只改常量不改解码，会把 HDD 判成 SSD（反向失效），正好破坏刚刚修好的 N3；
- 第 4、5 项让 relay 与路径已知性两道防线失效，属"平时不显形、异常时放大"的类型。

建议的关闭顺序：**1 → 3 → 2 → 4 → 5 → 6/7/8**。第 1、3 项修完后我可以只跑定向复验（`p0_4b_deep.py` + `p0_1f_verify_fix.py`，约 1 分钟），不需要再全量重跑。
