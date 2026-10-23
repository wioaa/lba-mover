# `StorageDeviceSeekPenaltyProperty` 取证记录（P0-1）

> 本文件保存 **原始 hex 证据**，用于钉死介质探测的常量、结构布局与布尔偏移。
> 结论一句话：**prop = 7 是对的，但描述符是 12 字节、布尔在 offset 8，
> 且 `Version` 字段不能用来做前置校验。**

## 1. 背景：被驳回的那个常量

v2 第一批曾把 `StorageDeviceSeekPenaltyProperty` 写成 `4`，理由链如下（**错的**）：

1. 误以为 `StorageDeviceUniqueIdProperty` = 3 且「prop=3 返回的是
   `STORAGE_ACCESS_ALIGNMENT_DESCRIPTOR` 形状」→ 推出 AccessAlignment 在 3；
2. 于是按 3 往后数，得到 SeekPenalty = 4。

第 1 步的观测其实是对的，但结构判读是错的：prop=3 返回的是
`STORAGE_DEVICE_UNIQUE_IDENTIFIER`（520 字节、`Version=1`、内部嵌套一个
`STORAGE_DEVICE_ID_DESCRIPTOR`），**不是** AccessAlignment。
真正的 AccessAlignment 是 **6**（见 §2 的 `BytesPerLogicalSector=512` /
`BytesPerPhysicalSector=4096`），枚举基线因此整体错位一格。

后果不是"值不准"，而是**功能失效**：`prop=4` 拿回的是
`DEVICE_WRITECACHE_DESCRIPTOR`（28 字节），把它的第 4 字节当
`IncursSeekPenalty` 属于读垃圾；再叠加下文 §3 的 `Version` 校验，实际结果是
`_query_seek_penalty()` **在本机恒返回 `None`**，介质一律降级为 `unknown`，
HDD 永远拿不到加速策略（N3 需求静默失效）。

## 2. 逐 prop 原始输出（卷 F:，ACASIS EC-7351，USB，真值 HDD）

输出缓冲区申请 **4096 字节**（关键：若按"期望 8 字节"申请，驱动只会回 8 字节，
`Size` 字段看起来「吻合」，反而掩盖真实布局）。

```
[0  StorageDeviceProperty]               ok=True  returned=396
    0000  28 00 00 00 8c 01 00 00 00 00 00 01 28 00 00 00   |(...........(...|
    0010  31 00 00 00 42 00 00 00 47 00 00 00 07 00 00 00   |1...B...G.......|
    -> Version=40  Size=396        BusType=7(USB) @28 ✓
[1  StorageAdapterProperty]              ok=True  returned=32
    0000  20 00 00 00 20 00 00 00 00 00 08 00 ff ff ff ff   | ... ...........|
    0010  03 00 00 00 01 00 01 01 07 00 02 00 00 00 00 00   |................|
    -> Version=32  Size=32         BusType=7 @24 ✓
[2  StorageDeviceIdProperty]             ok=True  returned=104
    0000  10 00 00 00 68 00 00 00 02 00 00 00 02 00 00 00   |....h...........|
    0010  01 00 00 00 2c 00 40 00 00 00 00 00 41 43 41 53   |....,.@.....ACAS|
    -> Version=16  Size=104        "ACASIS EC-7351" 可在 offset 28 附近读到
[3  StorageDeviceUniqueIdProperty]       ok=True  returned=520
    0000  01 00 00 00 08 02 00 00 14 00 00 00 7c 00 00 00   |............|...|
    -> Version=1   Size=520        注意：这个 prop 的 Version 真的是 1
[4  StorageDeviceWriteCacheProperty]     ok=True  returned=28
    0000  1c 00 00 00 1c 00 00 00 02 00 00 00 02 00 00 00   |................|
    0010  02 00 00 00 01 00 00 00 01 00 00 00               |............|
    -> Version=28  Size=28         被误用的就是它
[5  StorageMiniportProperty]             ok=True  returned=24
[6  StorageAccessAlignmentProperty]      ok=True  returned=28
    0000  1c 00 00 00 1c 00 00 00 00 00 00 00 00 00 00 00   |................|
    0010  00 02 00 00 00 10 00 00 00 00 00 00               |............|
    -> Version=28  Size=28
       按 STORAGE_ACCESS_ALIGNMENT_DESCRIPTOR 解读：
         BytesPerCacheLine            @8  = 0
         BytesOffsetForCacheAlignment @12 = 0
         BytesPerLogicalSector        @16 = 0x200 = 512   ✓
         BytesPerPhysicalSector       @20 = 0x1000 = 4096 ✓
       -> 结构完全吻合，AccessAlignment = 6 得到确认
[7  StorageDeviceSeekPenaltyProperty]    ok=True  returned=12
    0000  0c 00 00 00 0c 00 00 00 01 00 00 00               |............|
    -> Version=12  Size=12  bytes[8:12]=1
[8  StorageDeviceTrimProperty]           ok=True  returned=12
    0000  0c 00 00 00 0c 00 00 00 01 00 00 00               |............|
[9  StorageDeviceWriteAggregationProperty] ok=False returned=0  err=1
```

**要点**：offset 4 的 `Size` 字段在所有 prop、所有卷上都**精确等于**
`DeviceIoControl` 回的字节数（396/396、32/32、104/104、520/520、28/28、24/24、
28/28、12/12、12/12、119/119、139/139、120/120、140/140）。
所以 offset 0 是 `Version`、offset 4 是 `Size` **不会认错**。

## 3. 真实布局：12 字节，布尔在 offset 8

`prop=7` 的返回是 **12 字节**，不是 8：

```
0c 00 00 00 | 0c 00 00 00 | 01 00 00 00
Version=12    Size=12        IncursSeekPenalty=1 (BOOLEAN + 3 字节补齐)
```

即布局为 `{ Version(u32), Size(u32), IncursSeekPenalty(BOOLEAN) }` = 4+4+1+3 = 12。

两条踩坑点：

1. **布尔在 offset 8，不在 offset 4。** offset 4 是 `Size`（恒为 12，非 0），
   拿它当布尔会永远判成「有寻道惩罚」→ 所有盘都是 HDD。
2. **`Version` 在本机被驱动填成 12（= sizeof），不是 1。**
   因此「`Version != 1` 就放弃」的校验会让函数恒返回 `None`。
   对照组：`prop=3` 的 `Version` 确实是 1，说明该字段取值**因 prop 而异**，
   不能一概而论。

代码因此改为用**描述符自带的 `Size`** 判别布局（见
`app/win32/storage.py::decode_seek_penalty`）：

| `Size` | 布局 | 布尔偏移 |
| --- | --- | --- |
| `8` | `{Version, IncursSeekPenalty}` + 补齐 | 4 |
| `>= 12` | `{Version, Size, IncursSeekPenalty}` + 补齐 | 8 |

并额外要求该字节必须是 `0/1`，否则视为布局判错 → 返回 `None` 继续降级（不猜）。

## 4. 真值对照：`Get-PhysicalDisk` vs `prop=7`

真值取自 `Get-PhysicalDisk` / `Get-Partition`（与本工具完全无关的独立数据源）。
卷 → 物理盘映射由 `IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS` 取得。

| 卷 | 物理盘 / 型号 | 总线 | `Get-PhysicalDisk.MediaType` | `prop=7` 原始字节 | 判定 | 是否一致 |
| --- | --- | --- | --- | --- | --- | :-: |
| C: | disk 3 J.ZAO QL SERIES 1TB | NVMe | **SSD** | `0c 00 00 00 0c 00 00 00 00 00 00 00` | SSD | ✓ |
| D: | disk 2 TOSHIBA HDWD120 | SATA | **HDD** | `0c 00 00 00 0c 00 00 00 01 00 00 00` | HDD | ✓ |
| E: | disk 1 ST1000LM035-1RK172 | SATA | **HDD** | `0c 00 00 00 0c 00 00 00 01 00 00 00` | HDD | ✓ |
| F: | disk 6 ACASIS EC-7351 | USB | **HDD** | `0c 00 00 00 0c 00 00 00 01 00 00 00` | HDD | ✓ |
| G: | disk 4 WDC WDS250G2B0C | NVMe | **SSD** | `0c 00 00 00 0c 00 00 00 00 00 00 00` | SSD | ✓ |
| I: | disk 0 ST8000NM017B-2TJ103 | SATA | **HDD** | `0c 00 00 00 0c 00 00 00 01 00 00 00` | HDD | ✓ |
| H: | disk 5 TOSHIBA External USB 3.0 | USB | Unspecified | `err=1`（不支持） | unknown | — |
| K: | disk 7 Samsung Flash Drive | USB | Unspecified | `err=1`（不支持） | unknown | — |
| L: | disk 7 Samsung Flash Drive | USB | Unspecified | `err=1`（不支持） | unknown | — |
| R: | `disk=0xFFFFFFFF`（虚拟卷） | — | — | `0c 00 00 00 0c 00 00 00 00 00 00 00` | SSD | — |

**一致率 6/6**（所有能从两个来源同时拿到结论的卷）。

同一块物理盘上的不同卷结论一致：K: 与 L: 同在 disk 7，同为 `err=1`；
C:/G: 两块 NVMe SSD 同为 0；D:/E:/F:/I: 四块 HDD 同为 1。

驱动不支持该属性时（H:/K:/L:）返回 `ERROR_INVALID_FUNCTION(1)`，
本工具据此退回 `BusType` 判定 → USB 无法区分 → 如实显示「未知（保守模式）」，
**不会假装知道**。

## 5. 附带修掉的一个可用性问题

原先 `probe_media()` 一律用 `GENERIC_READ` 打开卷设备。非提权状态下
固定磁盘卷会返回 `ERROR_ACCESS_DENIED(5)`，于是 C:/D:/E:/I: 的介质全部
「未知」——即使在**提权后**才发现本来能读。

现改为 `open_volume_device()`：先 `GENERIC_READ`，遇 `ERROR_ACCESS_DENIED`
再退回 `access=0`（`IOCTL_STORAGE_QUERY_PROPERTY` /
`IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS` 都是 `FILE_ANY_ACCESS`，
零访问权限句柄足以查询，**不读一个字节**）。
修完后 C:/D:/E:/I: 在非提权下也能正确判定，实测同样 6/6。

## 6. 回归钉子

`tests/test_storage.py`：

* `test_seek_penalty_property_id_is_sdk_value` —— 常量必须 == 7，且显式断言
  `!= 4`、`!= 6`（有人改回 4 立刻红）；
* `test_decode_seek_penalty_header_layout` —— 12 字节布局，布尔在 offset 8；
* `test_decode_seek_penalty_compact_layout` —— 8 字节布局，布尔在 offset 4；
* `test_decode_seek_penalty_rejects_garbage` —— 布局不明 / 布尔非 0-1 → `None`；
* `test_volume_device_openable_without_elevation` —— 零访问权限必须能打开；
* `test_seek_penalty_source_is_honest` —— 声称 `seek_penalty` 就必须给出确定介质。
