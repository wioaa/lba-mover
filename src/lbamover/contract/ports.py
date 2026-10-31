"""契约层端口（Protocol）。

端口是引擎对**外部世界**的全部假设。把这些假设写成 Protocol 而不是
直接依赖具体实现，换来两件事：

* 引擎测试可以用内存假件跑，不需要真盘、不需要管理员；
* 真机实现（Windows 原始卷 / NTFS 搬迁）与模拟实现可以并存，
  由 ``devices.factory`` 在启动时决定注入哪个。
"""

from __future__ import annotations

from typing import Callable, Iterable, Protocol, runtime_checkable

from .events import Event
from .models import Extent, FileFrag, Geometry, VolumeInfo

__all__ = [
    "BlockDevice",
    "EventSink",
    "FileCatalog",
    "JobJournal",
    "VolumeSource",
]


@runtime_checkable
class BlockDevice(Protocol):
    """以 LBA 块为单位的裸设备读写。

    实现方必须保证（契约，不是建议）：

    * ``read_blocks`` 要么返回**恰好** ``count * block_size`` 字节，要么抛
      ``DeviceError``；**禁止**返回短数据。旧版返回短数据导致过静默截断。
    * ``write_blocks`` 只有在数据完整落盘后才返回；``None`` 返回值即成功。
    * 所有越界访问抛 ``DeviceError``，不允许静默裁剪。
    * 实现方自己处理线程安全——引擎可能从多个工作线程并发调用。
    """

    @property
    def geometry(self) -> Geometry: ...

    @property
    def name(self) -> str: ...

    @property
    def writable(self) -> bool: ...

    def read_blocks(self, lba: int, count: int) -> bytes: ...

    def write_blocks(self, lba: int, data: bytes) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class FileCatalog(Protocol):
    """把「卷上有哪些文件、各占哪些 LBA」提供给引擎。

    真机实现走 NTFS 的 ``FSCTL_GET_RETRIEVAL_POINTERS`` / MFT 读取；
    模拟实现直接读一份内存里的表。引擎只认这个接口，不关心来源。
    """

    def list_files(self, volume: VolumeInfo) -> Iterable[FileFrag]: ...

    def free_extents(self, volume: VolumeInfo) -> Iterable[Extent]: ...


@runtime_checkable
class EventSink(Protocol):
    """事件出口。UI 侧实现成 Qt 信号桥，CLI 侧实现成 stdout。"""

    def emit(self, event: Event) -> None: ...


@runtime_checkable
class JobJournal(Protocol):
    """执行流水（追加写）。用于断点续做与事后追溯。"""

    def append(self, record: dict) -> None: ...

    def records(self) -> list[dict]: ...


class NullSink:
    """丢弃一切事件。默认值，避免每个调用点都要写 ``if sink is not None``。"""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def emit(self, event: Event) -> None:
        self.count += 1


class ListSink:
    """把事件收进列表，测试与 CLI 用。"""

    __slots__ = ("events",)

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def of_kind(self, kind) -> list[Event]:
        return [e for e in self.events if e.kind == kind]

    def clear(self) -> None:
        self.events.clear()


#: 事件订阅回调（引擎内部用于把事件推给 sink 的轻量函数类型）。
EmitFn = Callable[[Event], None]
