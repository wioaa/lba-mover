"""视觉主题：一套色板 + 一份 QSS。

两个原则：

* **颜色只有一处定义**。所有控件从 :class:`Palette` 取值，绝不在控件里
  写死十六进制——否则改一次主题要翻十个文件，必然会漏。
* **深色为默认**。机械盘整理常在夜间跑，浅色大面积白底刺眼；同时提供
  ``light`` 供打印/截图场景。

QSS 的坑：Qt 不支持 flex/grid，也不支持 ``box-shadow``。圆角和描边只能靠
``border-radius`` + ``border``；复杂阴影用 ``QGraphicsDropShadowEffect``，
但那个在大量控件上会拖慢重绘，因此只在卡片上用一层很薄的描边代替。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

__all__ = ["Palette", "DARK", "LIGHT", "apply_theme", "mono_font", "ui_font", "heat_color"]


@dataclass(frozen=True, slots=True)
class Palette:
    name: str
    bg: str
    panel: str
    panel_alt: str
    border: str
    border_strong: str
    text: str
    text_dim: str
    text_faint: str
    accent: str
    accent_dim: str
    ok: str
    warn: str
    danger: str
    track: str
    free_block: str
    used_block: str


DARK = Palette(
    name="dark",
    bg="#0E1116",
    panel="#151A21",
    panel_alt="#1B222B",
    border="#232B36",
    border_strong="#2E3846",
    text="#E7ECF4",
    text_dim="#93A0B4",
    text_faint="#64718A",
    accent="#4C8DFF",
    accent_dim="#2C4E8F",
    ok="#35C08A",
    warn="#F2B138",
    danger="#F0596B",
    track="#1E252E",
    free_block="#1C2530",
    used_block="#3D6FD1",
)

LIGHT = Palette(
    name="light",
    bg="#F4F6FA",
    panel="#FFFFFF",
    panel_alt="#F0F3F8",
    border="#DDE3EC",
    border_strong="#C6CFDC",
    text="#161C26",
    text_dim="#5A6678",
    text_faint="#8B97A8",
    accent="#2563EB",
    accent_dim="#BFD3FA",
    ok="#0F9D68",
    warn="#B77800",
    danger="#D93A4B",
    track="#E4E9F1",
    free_block="#E8EEF7",
    used_block="#7EA6EE",
)


def heat_color(palette: Palette, ratio: float) -> QColor:
    """碎片热力：0 蓝 → 0.5 黄 → 1 红。线性插值，不做 gamma 修正以保持可读。"""
    r = max(0.0, min(1.0, ratio))
    low = QColor(palette.used_block)
    mid = QColor(palette.warn)
    high = QColor(palette.danger)
    if r < 0.5:
        return _mix(low, mid, r / 0.5)
    return _mix(mid, high, (r - 0.5) / 0.5)


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


_MONO_CANDIDATES = ("JetBrains Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono")
_UI_CANDIDATES = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "Noto Sans CJK SC")


def _pick(candidates: tuple[str, ...], fallback: str) -> str:
    available = set(QFontDatabase.families())
    for name in candidates:
        if name in available:
            return name
    return fallback


def mono_font(size: int = 10) -> QFont:
    f = QFont(_pick(_MONO_CANDIDATES, "monospace"), size)
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


def ui_font(size: int = 10, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    f = QFont(_pick(_UI_CANDIDATES, "sans-serif"), size)
    f.setWeight(weight)
    return f


def stylesheet(p: Palette) -> str:
    return f"""
* {{
    outline: none;
}}
QWidget {{
    background: transparent;
    color: {p.text};
    font-size: 13px;
}}
QMainWindow, QWidget#root {{
    background: {p.bg};
}}
QWidget#card {{
    background: {p.panel};
    border: 1px solid {p.border};
    border-radius: 12px;
}}
QLabel#cardTitle {{
    color: {p.text};
    font-size: 13px;
    font-weight: 600;
}}
QLabel#cardHint {{
    color: {p.text_faint};
    font-size: 11px;
}}
QLabel#sectionLabel {{
    color: {p.text_dim};
    font-size: 11px;
    letter-spacing: 0.4px;
}}
QLabel#heroValue {{
    color: {p.text};
    font-size: 32px;
    font-weight: 600;
}}
QLabel#metricValue {{
    color: {p.text};
    font-size: 17px;
    font-weight: 600;
}}
QLabel#metricKey {{
    color: {p.text_faint};
    font-size: 11px;
}}
QLabel#brand {{
    color: {p.text};
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 0.5px;
}}
QLabel#badge {{
    color: {p.text_dim};
    background: {p.panel_alt};
    border: 1px solid {p.border};
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 11px;
}}
QLabel#chip {{
    color: {p.text_dim};
    background: {p.panel_alt};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 3px 9px;
    font-size: 11px;
}}

/* ---------------- 按钮 ---------------- */
QPushButton {{
    background: {p.panel_alt};
    color: {p.text};
    border: 1px solid {p.border_strong};
    border-radius: 9px;
    padding: 8px 16px;
    font-size: 13px;
}}
QPushButton:hover {{
    border-color: {p.accent};
}}
QPushButton:pressed {{
    background: {p.track};
}}
QPushButton:disabled {{
    color: {p.text_faint};
    border-color: {p.border};
    background: {p.panel};
}}
QPushButton#primary {{
    background: {p.accent};
    color: #FFFFFF;
    border: 1px solid {p.accent};
    font-weight: 600;
}}
QPushButton#primary:hover {{
    background: {p.accent_dim};
}}
QPushButton#primary:disabled {{
    background: {p.track};
    color: {p.text_faint};
    border-color: {p.border};
}}
QPushButton#danger {{
    color: {p.danger};
    border-color: {p.danger};
    background: transparent;
}}
QPushButton#danger:hover {{
    background: rgba(240, 89, 107, 0.12);
}}
QPushButton#ghost {{
    background: transparent;
    border: 1px solid transparent;
    color: {p.text_dim};
    padding: 5px 9px;
}}
QPushButton#ghost:hover {{
    color: {p.text};
    background: {p.panel_alt};
}}
QPushButton#option {{
    text-align: left;
    background: transparent;
    border: 1px solid {p.border};
    border-radius: 9px;
    padding: 8px 11px;
    color: {p.text_dim};
    font-size: 12px;
}}
QPushButton#option:hover {{
    border-color: {p.border_strong};
    color: {p.text};
}}
QPushButton#option:checked {{
    border-color: {p.accent};
    background: {p.accent}1F;
    color: {p.text};
    font-weight: 600;
}}

/* ---------------- 列表 / 卷卡片 ---------------- */
QListWidget {{
    background: transparent;
    border: none;
}}
QListWidget::item {{
    border-radius: 10px;
    margin: 2px 0;
}}
QListWidget::item:selected {{
    background: {p.panel_alt};
    border: 1px solid {p.accent};
}}
QListWidget::item:hover {{
    background: {p.panel_alt};
}}

/* ---------------- 输入 ---------------- */
QSpinBox, QDoubleSpinBox, QComboBox, QLineEdit {{
    background: {p.panel_alt};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 5px 8px;
    color: {p.text};
    selection-background-color: {p.accent};
}}
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
    border-color: {p.border_strong};
}}
QComboBox QAbstractItemView {{
    background: {p.panel_alt};
    border: 1px solid {p.border_strong};
    selection-background-color: {p.accent};
    color: {p.text};
    outline: none;
}}
QCheckBox {{
    color: {p.text_dim};
    spacing: 7px;
}}
QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border-radius: 4px;
    border: 1px solid {p.border_strong};
    background: {p.panel_alt};
}}
QCheckBox::indicator:checked {{
    background: {p.accent};
    border-color: {p.accent};
}}
QSlider::groove:horizontal {{
    height: 4px;
    background: {p.track};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {p.accent};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 12px;
    height: 12px;
    margin: -5px 0;
    border-radius: 6px;
    background: {p.text};
}}

/* ---------------- 进度条 ---------------- */
QProgressBar {{
    background: {p.track};
    border: none;
    border-radius: 5px;
    height: 10px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: {p.accent};
    border-radius: 5px;
}}

/* ---------------- 日志 ---------------- */
QPlainTextEdit#log {{
    background: {p.panel_alt};
    border: 1px solid {p.border};
    border-radius: 9px;
    color: {p.text_dim};
    padding: 6px;
    selection-background-color: {p.accent_dim};
}}

/* ---------------- 分隔与滚动条 ---------------- */
QSplitter::handle {{
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 9px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {p.border_strong};
    border-radius: 4px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p.text_faint};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 9px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {p.border_strong};
    border-radius: 4px;
    min-width: 24px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}
QToolTip {{
    background: {p.panel_alt};
    color: {p.text};
    border: 1px solid {p.border_strong};
    padding: 5px 8px;
    border-radius: 6px;
}}
"""


def apply_theme(app: QApplication, palette: Palette = DARK) -> None:
    """把主题装到 QApplication 上。"""
    app.setStyle("Fusion")
    qp = QPalette()
    qp.setColor(QPalette.ColorRole.Window, QColor(palette.bg))
    qp.setColor(QPalette.ColorRole.WindowText, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.Base, QColor(palette.panel_alt))
    qp.setColor(QPalette.ColorRole.AlternateBase, QColor(palette.panel))
    qp.setColor(QPalette.ColorRole.Text, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.Button, QColor(palette.panel_alt))
    qp.setColor(QPalette.ColorRole.ButtonText, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.Highlight, QColor(palette.accent))
    qp.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    qp.setColor(QPalette.ColorRole.ToolTipBase, QColor(palette.panel_alt))
    qp.setColor(QPalette.ColorRole.ToolTipText, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.PlaceholderText, QColor(palette.text_faint))
    app.setPalette(qp)
    app.setFont(ui_font(10))
    app.setStyleSheet(stylesheet(palette))
