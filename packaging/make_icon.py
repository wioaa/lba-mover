"""生成程序图标（多尺寸 .ico）。

为什么要自己画：默认的 PyInstaller 图标是一个通用空白方块，在任务栏和
资源管理器里和一堆 Python 脚本 exe 撞脸。图省事贴个现成 ico 又会有版权
问题，所以用项目自己的色板画一个。

设计含义：三条 LBA「块带」——最上面那条被切成两段（碎片），下面两条是
连续的一整段（整理后）。16px 下也能看出"整 / 碎"的差别，这是图标的底线。

用法::

    python packaging/make_icon.py            # 输出 packaging/lbamover.ico
"""

from __future__ import annotations

import io
import os
import struct
import sys

_SIZES = (16, 24, 32, 48, 64, 128, 256)

_BG = "#111721"
_BG_EDGE = "#1F2A38"
_BLOCK = "#4C8DFF"
_BLOCK_DIM = "#2C4E8F"
_FRAG = "#F2B138"


def render(size: int) -> bytes:
    """画一枚图标，返回 PNG 字节。"""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPen

    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    r = QRectF(0, 0, size, size)
    radius = size * 0.22
    grad = QLinearGradient(0, 0, 0, size)
    grad.setColorAt(0.0, QColor(_BG_EDGE))
    grad.setColorAt(1.0, QColor(_BG))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(r, radius, radius)

    # 三条块带：第一条断成两段（碎片），后两条连续
    pad = size * 0.18
    bar_h = size * 0.13
    gap_v = size * 0.09
    usable = size - pad * 2
    y = pad + size * 0.06

    # 第 1 条：两段，中间留缝
    seg1 = usable * 0.50
    seg2 = usable * 0.34
    p.setBrush(QColor(_BLOCK))
    p.drawRoundedRect(QRectF(pad, y, seg1, bar_h), bar_h * 0.35, bar_h * 0.35)
    p.setBrush(QColor(_FRAG))
    p.drawRoundedRect(
        QRectF(pad + seg1 + size * 0.10, y, max(seg2, size * 0.06), bar_h),
        bar_h * 0.35,
        bar_h * 0.35,
    )

    # 第 2、3 条：连续
    y2 = y + bar_h + gap_v
    p.setBrush(QColor(_BLOCK))
    p.drawRoundedRect(QRectF(pad, y2, usable * 0.90, bar_h), bar_h * 0.35, bar_h * 0.35)
    y3 = y2 + bar_h + gap_v
    p.setBrush(QColor(_BLOCK_DIM))
    p.drawRoundedRect(QRectF(pad, y3, usable * 0.80, bar_h), bar_h * 0.35, bar_h * 0.35)

    if size >= 48:
        # 大尺寸才画描边，小尺寸描边会糊成一团
        pen = QPen(QColor(255, 255, 255, 28))
        pen.setWidthF(size * 0.012)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r.adjusted(1, 1, -1, -1), radius, radius)

    p.end()

    buf = io.BytesIO()
    # Qt 自带的 PNG 写入器：不引额外依赖
    from PySide6.QtCore import QBuffer
    from PySide6.QtGui import QImageWriter

    qbuf = QBuffer()
    qbuf.open(QBuffer.OpenModeFlag.WriteOnly)
    writer = QImageWriter(qbuf, b"png")
    writer.setQuality(100)
    if not writer.write(img):
        raise RuntimeError(f"写入 PNG 失败：{writer.errorString()}")
    return bytes(qbuf.data())


def build_ico(path: str) -> str:
    """把多个尺寸的 PNG 打包成 ICO 容器（Vista+ 支持 PNG 负载）。"""
    images = [(s, render(s)) for s in _SIZES]

    header = struct.pack("<HHH", 0, 1, len(images))  # reserved, type=icon, count
    entries = b""
    payload = b""
    offset = len(header) + 16 * len(images)
    for size, data in images:
        # 宽度/高度为 256 时写 0
        dim = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset + len(payload)
        )
        payload += data

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(header + entries + payload)
    return path


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out = argv[0] if argv else os.path.join(os.path.dirname(__file__), "lbamover.ico")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    _ = QGuiApplication.instance() or QGuiApplication([])
    p = build_ico(out)
    print(f"icon -> {p} ({os.path.getsize(p)} bytes, sizes={list(_SIZES)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
