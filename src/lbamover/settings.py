"""应用配置的持久化：让"上次怎么设的"活到下一次启动。

## 为什么不用 QSettings

QSettings 在 Windows 上写注册表：用户看不到、拷不走、卸载残留，出问题时
既不能"把配置文件发给我看看"，也不能"手动改一行试试"。这个工具的参数里
有一半是**危险参数**（搬运方式、强度、半锁定），把它们放在一个不可见、
不可导出、不可版本比对的地方不合适。

所以用一份 JSON：人可读、可 diff、可备份、可以贴进 issue。

## 存哪

=====================================  ==========================================
平台                                    路径
=====================================  ==========================================
Windows                                ``%APPDATA%\\LBAMover\\settings.json``
macOS / Linux                          ``$XDG_CONFIG_HOME/lbamover/settings.json``
（任意平台）设了 ``LBAMOVER_CONFIG``    该变量的值（**完整文件路径**）
=====================================  ==========================================

``LBAMOVER_CONFIG`` 是给便携版和测试用的：便携版把它指到 exe 同级目录，
配置就跟盘走；测试把它指到临时目录，绝不碰真实用户配置。

**不写仓库目录**（``.lbamover/``）：那里放断点与流水，属于"运行输出"，
和"用户偏好"是两种东西 —— 混在一起会让 ``git clean`` 顺手清掉设置。

## 什么时候写

1. **改动后 800ms**：防抖。拖一次并发数的滑块会连发十几个 ``valueChanged``，
   每次都落盘既慢又毫无意义；而 800ms 足够短，进程被强杀也只丢最近一次
   改动。
2. **关窗时强制落盘**：正常退出的路径上一定写成功一次，不依赖定时器。
3. **不做"退出前才写"**：那样崩一次就回到默认值，而这正是要修的问题。

写盘一律**原子替换**（临时文件 + ``os.replace``）：直接覆写时断电会得到
半截 JSON，下次启动只能当损坏处理，等于设置全丢。

## 坏了怎么办

配置是**便利功能**，任何异常都不能挡住主流程：

* 文件不存在 → 全默认，不报错（第一次启动就是这种情况）；
* JSON 语法错 → 备份成 ``settings.json.broken-<时间戳>``，然后按默认启动。
  **不静默删除**：用户改过的东西，删掉就再也查不出为什么"设置没生效"；
* 单个字段非法（例如手工把 ``chunk_blocks`` 改成 99999）→ 只丢这一个字段，
  其余照常恢复，并在日志里点名是哪个；
* 目录不可写 / 磁盘满 → 记一条警告，界面照常用。下次改配置会再试。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from pathlib import Path
from typing import Any, get_type_hints

from .contract.enums import Intensity, Strategy
from .contract.models import JobParams

__all__ = [
    "AppSettings",
    "SettingsStore",
    "Loaded",
    "SCHEMA_VERSION",
    "default_config_path",
    "DEFAULT_CONFIG_DIRNAME",
]

#: 配置格式版本。**只在结构不兼容时加**，加字段不用动它。
SCHEMA_VERSION = 1

DEFAULT_CONFIG_DIRNAME = "LBAMover"
FILE_NAME = "settings.json"

#: 这些字段要在 ``exec_mode`` **之前**应用。
#:
#: 因为 ``JobParams`` 有一条跨字段校验：``exec_mode=CACHED`` 要求
#: ``cache_dir`` 非空。逐字段恢复时若先设 ``exec_mode``，那一步会被判非法
#: 丢掉，用户看到的是"选了跨盘缓存，重开变成盘内搬运"。
_PARAMS_APPLY_FIRST = ("cache_dir", "mem_cache_blocks", "mem_cache_block_kib")


def default_config_path() -> Path:
    """当前平台的配置文件路径。见模块文档里的对照表。"""
    env = os.environ.get("LBAMOVER_CONFIG")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / DEFAULT_CONFIG_DIRNAME / FILE_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(xdg) / "lbamover" / FILE_NAME


@dataclass(frozen=True, slots=True)
class AppSettings:
    """一份完整的用户偏好。**只有"下次启动该长什么样"才放这里。**

    不存的东西也有讲究：扫描结果、计划、断点属于"任务状态"，它们已经各有
    自己的落盘位置（``.lbamover/``）。把几万条文件记录塞进设置文件，只会让
    启动读一个几十 MB 的 JSON。
    """

    #: 上次用的整理模式。
    strategy: Strategy = Strategy.COALESCE
    #: 全部运行参数（强度也在里面）。
    params: JobParams = field(default_factory=JobParams)
    #: 上次的目标卷（``device_path``）。卷不在时会退回第一块盘并提示。
    volume_path: str = ""
    #: 左栏是否展开。窄屏时程序会自动收起，用户手动收起的偏好也要记住。
    left_panel_visible: bool = True
    #: 窗口尺寸。0 表示用默认值（不记住）。
    window_width: int = 0
    window_height: int = 0

    # -- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            # 存的是**日期时间**而不是版本号：排查"设置为什么没生效"时，
            # 第一件想知道的事是这份文件什么时候写的。
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "strategy": self.strategy.value,
            "params": self.params.to_dict(),
            "volume_path": self.volume_path,
            "left_panel_visible": self.left_panel_visible,
            "window_width": self.window_width,
            "window_height": self.window_height,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> tuple["AppSettings", list[str]]:
        """宽松解析。返回 ``(设置, 说明列表)``；说明里每条都是"哪个没用上、为什么"。"""
        notes: list[str] = []
        if not isinstance(raw, dict):
            return cls(), ["配置文件内容不是一个对象，已按默认值启动"]

        schema = raw.get("schema")
        if isinstance(schema, int) and schema > SCHEMA_VERSION:
            notes.append(
                f"配置来自更新的版本（schema {schema} > {SCHEMA_VERSION}），"
                "未知字段已忽略"
            )

        base = cls()
        strategy = base.strategy
        raw_strategy = raw.get("strategy")
        if raw_strategy is not None:
            try:
                strategy = Strategy(raw_strategy)
            except ValueError:
                notes.append(f"整理模式 {raw_strategy!r} 不存在，已用默认值")

        params, param_notes = _params_from(raw.get("params"))
        notes.extend(param_notes)

        volume_path = raw.get("volume_path")
        if not isinstance(volume_path, str):
            volume_path = ""

        left = raw.get("left_panel_visible")
        left_visible = left if isinstance(left, bool) else base.left_panel_visible

        width = _bounded_int(raw.get("window_width"), 0, 20000, notes, "窗口宽度")
        height = _bounded_int(raw.get("window_height"), 0, 20000, notes, "窗口高度")

        return (
            cls(
                strategy=strategy,
                params=params,
                volume_path=volume_path,
                left_panel_visible=left_visible,
                window_width=width,
                window_height=height,
            ),
            notes,
        )


def _bounded_int(value: Any, lo: int, hi: int, notes: list[str], label: str) -> int:
    if value is None:
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        notes.append(f"{label} {value!r} 不是整数，已忽略")
        return 0
    if not (lo <= n <= hi):
        notes.append(f"{label} {n} 超出 {lo}..{hi}，已忽略")
        return 0
    return n


def _params_from(raw: Any) -> tuple[JobParams, list[str]]:
    """逐字段恢复 ``JobParams``。

    为什么不是一句 ``JobParams.from_dict(raw)``：那样任何一个字段越界
    （手工改错一个数字、老版本留下的非法组合）都会让**整份**参数回默认 ——
    用户改过的十几个选项一起消失，而日志里只有一句"参数非法"。

    这里从默认值出发，逐个字段 ``replace`` 并单独 try：坏的那个丢掉、点出
    名字，好的照常生效。
    """
    notes: list[str] = []
    if not isinstance(raw, dict):
        return JobParams(), notes

    known = {f.name: f for f in fields(JobParams)}
    hints = get_type_hints(JobParams)
    coerced: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in known:
            # 老版本留下的已删除字段：静默忽略，不算错
            continue
        try:
            coerced[key] = _coerce(hints.get(key), value)
        except (TypeError, ValueError):
            notes.append(f"参数「{key}」的值 {value!r} 无法解析，已用默认值")

    # 先应用"被别的字段依赖"的那些（见 _PARAMS_APPLY_FIRST 的说明）
    ordered = [k for k in _PARAMS_APPLY_FIRST if k in coerced]
    ordered += [k for k in coerced if k not in _PARAMS_APPLY_FIRST]

    params = JobParams()
    for key in ordered:
        try:
            params = replace(params, **{key: coerced[key]})
        except ValueError as exc:
            notes.append(f"参数「{key}」不合法（{exc}），已用默认值")
    return params, notes


def _coerce(want: Any, value: Any) -> Any:
    """按类型注解把 JSON 值还原成目标类型。枚举用 ``.value`` 存、按类转回。"""
    if isinstance(want, type) and issubclass(want, Enum):
        return want(value)
    if want is int:
        return int(value)
    if want is float:
        return float(value)
    if want is bool:
        if isinstance(value, bool):
            return value
        raise TypeError("布尔字段收到非布尔值")
    if want is str:
        if not isinstance(value, str):
            raise TypeError("字符串字段收到非字符串")
        return value
    return value


@dataclass(slots=True)
class Loaded:
    """``load()`` 的结果。``notes`` 要原样显示到日志里，别吞。"""

    settings: AppSettings
    notes: list[str]
    #: 读取过程中是否发现文件损坏（已备份）。界面应提示"设置已重置"。
    recovered: bool = False
    #: 配置文件是否本来就不存在（第一次启动）
    missing: bool = False


class SettingsStore:
    """读写一个配置文件。**所有方法都不抛**（除了传错类型），错误走返回值。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_config_path()
        #: 上一次保存失败的说明。界面据此提示"这次没存上"，而不是假装成功。
        self.last_error = ""

    # -- 读 ---------------------------------------------------------------

    def load(self) -> Loaded:
        if not self.path.exists():
            return Loaded(AppSettings(), [], missing=True)
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            return Loaded(AppSettings(), [f"配置文件读不了（{exc}），已按默认值启动"], recovered=True)
        try:
            raw = json.loads(text)
        except (ValueError, UnicodeDecodeError) as exc:
            backup = self._quarantine()
            where = f"，原文件已备份为 {backup.name}" if backup else "（备份也失败了）"
            return Loaded(
                AppSettings(),
                [f"配置文件不是合法 JSON（{exc}）{where}"],
                recovered=True,
            )
        settings, notes = AppSettings.from_dict(raw)
        return Loaded(settings, notes)

    def _quarantine(self) -> Path | None:
        """把坏文件改名留证。删掉就再也查不出"为什么设置没生效"。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.name}.broken-{stamp}")
        try:
            os.replace(self.path, target)
            return target
        except OSError:
            return None

    # -- 写 ---------------------------------------------------------------

    def save(self, settings: AppSettings) -> str:
        """原子写。返回空串表示成功，否则是给用户看的说明。"""
        payload = json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n"
        directory = self.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.last_error = f"配置目录建不了（{exc}）：{directory}"
            return self.last_error

        tmp_name = ""
        try:
            # 临时文件必须与目标**同一个目录**：跨盘的 os.replace 不是原子的。
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=str(directory)
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
            tmp_name = ""
            self.last_error = ""
            return ""
        except OSError as exc:
            self.last_error = f"配置保存失败（{exc}）"
            return self.last_error
        finally:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def clear(self) -> str:
        try:
            self.path.unlink()
            return ""
        except FileNotFoundError:
            return ""
        except OSError as exc:
            return f"配置文件删不掉（{exc}）"
