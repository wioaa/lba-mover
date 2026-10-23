"""主题：浅色 / 深色调色板 + 全局 QSS。

设计约定：
* 间距一律走 8px 栅格；
* 卡片 = 白底 + 1px 极浅描边 + 轻阴影（阴影由控件自行绘制，不用重投影）；
* 所有 Qt 原生控件的默认灰色外观都被 QSS 覆盖；
* 自定义绘制的控件通过 :func:`palette` 读取当前调色板。
"""

from __future__ import annotations

from typing import Dict, List

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase

# --------------------------------------------------------------------------- #
# 调色板
# --------------------------------------------------------------------------- #
LIGHT: Dict[str, str] = {
    "name": "light",
    "bg": "#EEF2F7",
    "card": "#FFFFFF",
    "sunken": "#F4F7FB",
    "elevated": "#FFFFFF",
    "border": "#E4EAF2",
    "border_strong": "#CBD5E1",
    "text": "#0F172A",
    "text_dim": "#5B6B82",
    "text_faint": "#94A3B8",
    "accent": "#2563EB",
    "accent_hover": "#1D4ED8",
    "accent_press": "#1E40AF",
    "accent_soft": "#E8F0FE",
    "accent_soft2": "#D7E4FD",
    "accent_contrast": "#FFFFFF",
    "cyan": "#0EA5E9",
    "danger": "#DC2626",
    "danger_hover": "#B91C1C",
    "danger_soft": "#FEE9E9",
    "success": "#16A34A",
    "success_soft": "#E6F6EC",
    "warning": "#D97706",
    "warning_soft": "#FDF3E3",
    "hover": "#EEF3FA",
    "pressed": "#E2E9F4",
    "disabled_bg": "#F1F5F9",
    "scroll": "#CFDAE8",
    "scroll_hover": "#B0C2D8",
    "occupied": "#A8B8CE",
    "occupied_deep": "#7F97B8",
    "free": "#EDF2F9",
    "mft": "#C3D0E2",
    "grid": "#E8EEF6",
    "zebra": "#FAFCFE",
    "shadow": "#0F172A",
    "shadow_alpha": "46",
    "on_accent_dim": "#D7E3FF",
    # ---- v2 布局图新增语义色（与 占用/空闲/选中 三色互不混淆）---- #
    #: 目标区间：紫罗兰（现有已占用=灰蓝、空闲=近白、选中文件=蓝→青渐变）
    "target": "#7C3AED",
    "target_soft": "#F2EBFE",
    #: 挡路者：橙（与 warning 的琥珀 #D97706 拉开明度/色相）
    "blocker": "#EA580C",
    "blocker_soft": "#FDEDE4",
    #: 保留区（卷首/卷尾）：石板灰，靠斜纹 + 锁形图标区分于已占用
    "reserve": "#64748B",
    "reserve_soft": "#EEF1F6",
}

DARK: Dict[str, str] = {
    "name": "dark",
    "bg": "#0B0F16",
    "card": "#151B24",
    "sunken": "#10161E",
    "elevated": "#1B2330",
    "border": "#232C3A",
    "border_strong": "#33415A",
    "text": "#E8EEF7",
    "text_dim": "#9AACC2",
    "text_faint": "#6B7C93",
    "accent": "#3B82F6",
    "accent_hover": "#60A5FA",
    "accent_press": "#2563EB",
    "accent_soft": "#182742",
    "accent_soft2": "#1E3355",
    "accent_contrast": "#FFFFFF",
    "cyan": "#22D3EE",
    "danger": "#EF4444",
    "danger_hover": "#DC2626",
    "danger_soft": "#2A1618",
    "success": "#22C55E",
    "success_soft": "#12271A",
    "warning": "#F59E0B",
    "warning_soft": "#2A2011",
    "hover": "#1C242F",
    "pressed": "#232E3D",
    "disabled_bg": "#171E28",
    "scroll": "#2B3748",
    "scroll_hover": "#3A4A5F",
    "occupied": "#3E4C61",
    "occupied_deep": "#55677F",
    "free": "#1A222D",
    "mft": "#2A3547",
    "grid": "#1B232F",
    "zebra": "#181F29",
    "shadow": "#000000",
    "shadow_alpha": "120",
    "on_accent_dim": "#9DBBEF",
    # ---- v2 布局图新增语义色（深色下提高明度、降低饱和，避免刺眼）---- #
    "target": "#A78BFA",
    "target_soft": "#251C3F",
    "blocker": "#FB923C",
    "blocker_soft": "#2E1B10",
    "reserve": "#7C8CA3",
    "reserve_soft": "#1D232C",
}

PALETTES: Dict[str, Dict[str, str]] = {"light": LIGHT, "dark": DARK}

FONT_STACK: List[str] = ["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "Arial"]
MONO_STACK: List[str] = ["Cascadia Mono", "Consolas", "JetBrains Mono", "Courier New"]

# 间距栅格
SPACE = 8
RADIUS_CARD = 12
RADIUS_CTRL = 8


def pick_font() -> str:
    """返回系统中实际可用的首选字体名。"""
    families = set(QFontDatabase.families())
    for name in FONT_STACK:
        if name in families:
            return name
    # 字体库为空（例如离屏平台）时仍使用首选名字，交给 Qt 在渲染期解析
    return FONT_STACK[0]


def pick_mono_font() -> str:
    """返回系统中可用的等宽字体名。"""
    families = set(QFontDatabase.families())
    for name in MONO_STACK:
        if name in families:
            return name
    return MONO_STACK[1]


# --------------------------------------------------------------------------- #
# 主题管理
# --------------------------------------------------------------------------- #
class ThemeManager(QObject):
    """全局主题状态（单例）。"""

    changed = Signal(str)

    _instance: "ThemeManager | None" = None

    def __init__(self) -> None:
        super().__init__()
        self._mode = "light"
        self._font_family = "Microsoft YaHei UI"
        self._mono_family = "Consolas"

    @classmethod
    def instance(cls) -> "ThemeManager":
        """获取单例。"""
        if cls._instance is None:
            cls._instance = ThemeManager()
        return cls._instance

    # ---------------- 状态 ---------------- #
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def is_dark(self) -> bool:
        return self._mode == "dark"

    def palette(self) -> Dict[str, str]:
        return PALETTES[self._mode]

    def color(self, key: str, fallback: str = "#000000") -> QColor:
        """按 key 取 ``QColor``。"""
        return QColor(self.palette().get(key, fallback))

    def font_family(self) -> str:
        return self._font_family

    def mono_family(self) -> str:
        return self._mono_family

    # ---------------- 切换 ---------------- #
    def set_mode(self, mode: str, apply_to=None) -> None:
        """切换主题并重新应用样式表。"""
        if mode not in PALETTES or mode == self._mode:
            if apply_to is not None:
                apply_to.setStyleSheet(build_stylesheet(self.palette(), self._font_family))
            return
        self._mode = mode
        if apply_to is not None:
            apply_to.setStyleSheet(build_stylesheet(self.palette(), self._font_family))
        self.changed.emit(mode)

    def toggle(self, apply_to=None) -> str:
        """在浅色 / 深色之间切换，返回新模式。"""
        self.set_mode("dark" if self._mode == "light" else "light", apply_to)
        return self._mode

    def prepare_fonts(self) -> None:
        """探测系统可用字体（须在 QApplication 创建后调用）。"""
        self._font_family = pick_font()
        self._mono_family = pick_mono_font()


_THEME = ThemeManager.instance()


def theme() -> ThemeManager:
    """访问全局主题管理器。"""
    return _THEME


def palette() -> Dict[str, str]:
    """当前调色板字典。"""
    return _THEME.palette()


def color(key: str, fallback: str = "#000000") -> QColor:
    """当前调色板中的颜色。"""
    return _THEME.color(key, fallback)


def app_font(size: int = 13, weight: int = QFont.Weight.Normal) -> QFont:
    """构造一个遵循主题的字体。"""
    font = QFont(_THEME.font_family())
    font.setPixelSize(size)
    font.setWeight(weight)
    return font


def mono_font(size: int = 12) -> QFont:
    """等宽字体（日志用）。"""
    font = QFont(_THEME.mono_family())
    font.setPixelSize(size)
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font


# --------------------------------------------------------------------------- #
# QSS
# --------------------------------------------------------------------------- #
def build_stylesheet(p: Dict[str, str], font_family: str) -> str:
    """根据调色板生成完整 QSS。"""
    return f"""
/* ===================== 基础 ===================== */
QWidget {{
    color: {p['text']};
    font-family: "{font_family}";
    font-size: 13px;
    selection-background-color: {p['accent_soft2']};
    selection-color: {p['text']};
}}
QToolTip {{
    background-color: {p['elevated']};
    color: {p['text']};
    border: 1px solid {p['border_strong']};
    border-radius: 6px;
    padding: 6px 9px;
    font-size: 12px;
}}

/* ===================== 文本层级 ===================== */
QLabel {{ background: transparent; }}
QLabel[role="h1"] {{ font-size: 20px; font-weight: 700; letter-spacing: 0.2px; }}
QLabel[role="h2"] {{ font-size: 15px; font-weight: 700; }}
QLabel[role="h3"] {{ font-size: 13px; font-weight: 700; }}
QLabel[role="body"] {{ font-size: 13px; }}
QLabel[role="dim"] {{ font-size: 12px; color: {p['text_dim']}; }}
QLabel[role="caption"] {{ font-size: 11px; color: {p['text_faint']}; }}
QLabel[role="value"] {{ font-size: 18px; font-weight: 700; }}
QLabel[role="danger"] {{ color: {p['danger']}; }}
QLabel[role="success"] {{ color: {p['success']}; }}
QLabel[role="warning"] {{ color: {p['warning']}; }}

/* ===================== 按钮 ===================== */
QPushButton {{
    background-color: {p['sunken']};
    color: {p['text']};
    border: 1px solid {p['border']};
    border-radius: {RADIUS_CTRL}px;
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 600;
    min-height: 18px;
}}
QPushButton:hover {{
    background-color: {p['hover']};
    border-color: {p['border_strong']};
}}
QPushButton:pressed {{
    background-color: {p['pressed']};
}}
QPushButton:disabled {{
    background-color: {p['disabled_bg']};
    color: {p['text_faint']};
    border-color: {p['border']};
}}
QPushButton[role="primary"] {{
    background-color: {p['accent']};
    color: {p['accent_contrast']};
    border: 1px solid {p['accent']};
}}
QPushButton[role="primary"]:hover {{
    background-color: {p['accent_hover']};
    border-color: {p['accent_hover']};
}}
QPushButton[role="primary"]:pressed {{
    background-color: {p['accent_press']};
    border-color: {p['accent_press']};
}}
QPushButton[role="primary"]:disabled {{
    background-color: {p['accent_soft2']};
    color: {p['text_faint']};
    border-color: transparent;
}}
QPushButton[role="danger"] {{
    background-color: {p['danger']};
    color: #FFFFFF;
    border: 1px solid {p['danger']};
}}
QPushButton[role="danger"]:hover {{
    background-color: {p['danger_hover']};
    border-color: {p['danger_hover']};
}}
QPushButton[role="danger"]:pressed {{
    background-color: {p['danger_hover']};
}}
QPushButton[role="danger"]:disabled {{
    background-color: {p['danger_soft']};
    color: {p['text_faint']};
    border-color: transparent;
}}
QPushButton[role="ghost"] {{
    background-color: transparent;
    border: 1px solid transparent;
    color: {p['text_dim']};
    font-weight: 600;
}}
QPushButton[role="ghost"]:hover {{
    background-color: {p['hover']};
    color: {p['text']};
}}
QPushButton[role="ghost"]:pressed {{
    background-color: {p['pressed']};
}}
QPushButton[role="ghost"]:disabled {{
    background-color: transparent;
    color: {p['text_faint']};
}}

/* ===================== 卡片 ===================== */
QFrame[role="card"] {{
    background-color: {p['card']};
    border: 1px solid {p['border']};
    border-radius: {RADIUS_CARD}px;
}}
QFrame[role="sunken"] {{
    background-color: {p['sunken']};
    border: 1px solid {p['border']};
    border-radius: 10px;
}}
QFrame[role="hline"] {{
    background-color: {p['border']};
    border: none;
    max-height: 1px;
    min-height: 1px;
}}
QFrame[role="vline"] {{
    background-color: {p['border']};
    border: none;
    max-width: 1px;
    min-width: 1px;
}}

/* ===================== 下拉框 ===================== */
QComboBox {{
    background-color: {p['card']};
    color: {p['text']};
    border: 1px solid {p['border']};
    border-radius: {RADIUS_CTRL}px;
    padding: 7px 34px 7px 12px;
    min-height: 20px;
    font-size: 13px;
}}
QComboBox:hover {{ border-color: {p['border_strong']}; }}
QComboBox:focus {{ border-color: {p['accent']}; }}
QComboBox:disabled {{ background-color: {p['disabled_bg']}; color: {p['text_faint']}; }}
QComboBox::drop-down {{ border: none; width: 30px; background: transparent; }}
QComboBox::down-arrow {{ image: none; width: 0px; height: 0px; }}
QComboBox QAbstractItemView {{
    background-color: {p['card']};
    border: 1px solid {p['border_strong']};
    border-radius: 8px;
    padding: 6px;
    outline: 0;
    selection-background-color: {p['accent_soft']};
    selection-color: {p['accent']};
}}
QComboBox QAbstractItemView::item {{
    padding: 8px 10px;
    border-radius: 6px;
    min-height: 20px;
}}
QComboBox QAbstractItemView::item:hover {{
    background-color: {p['hover']};
}}

/* ===================== 输入框 ===================== */
QLineEdit {{
    background-color: {p['card']};
    color: {p['text']};
    border: 1px solid {p['border']};
    border-radius: {RADIUS_CTRL}px;
    padding: 6px 10px;
    min-height: 20px;
    font-size: 13px;
    selection-background-color: {p['accent']};
    selection-color: {p['card']};
}}
QLineEdit:hover {{ border-color: {p['border_strong']}; }}
QLineEdit:focus {{ border-color: {p['accent']}; }}
QLineEdit:disabled {{ background-color: {p['disabled_bg']}; color: {p['text_faint']}; }}

/* ===================== 数字框 ===================== */
QSpinBox, QDoubleSpinBox {{
    background-color: {p['card']};
    color: {p['text']};
    border: 1px solid {p['border']};
    border-radius: {RADIUS_CTRL}px;
    padding: 6px 10px;
    min-height: 20px;
    font-size: 13px;
}}
QSpinBox:hover, QDoubleSpinBox:hover {{ border-color: {p['border_strong']}; }}
QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {p['accent']}; }}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 0px;
    border: none;
    background: transparent;
}}
QSpinBox::up-arrow, QSpinBox::down-arrow,
QDoubleSpinBox::up-arrow, QDoubleSpinBox::down-arrow {{
    width: 0px;
    height: 0px;
    image: none;
}}

/* ===================== 滑块 ===================== */
QSlider {{ background: transparent; min-height: 22px; }}
QSlider::groove:horizontal {{
    height: 6px;
    background-color: {p['sunken']};
    border: 1px solid {p['border']};
    border-radius: 3px;
}}
QSlider::sub-page:horizontal {{
    background-color: {p['accent']};
    border: 1px solid {p['accent']};
    border-radius: 3px;
}}
QSlider::add-page:horizontal {{
    background-color: {p['sunken']};
    border: 1px solid {p['border']};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    width: 14px;
    height: 14px;
    margin: -6px 0px;
    border-radius: 8px;
    background-color: {p['card']};
    border: 2px solid {p['accent']};
}}
QSlider::handle:horizontal:hover {{
    background-color: {p['accent_soft']};
}}
QSlider::handle:horizontal:pressed {{
    background-color: {p['accent']};
}}
QSlider::handle:horizontal:disabled {{
    border-color: {p['border_strong']};
    background-color: {p['disabled_bg']};
}}

/* ===================== 复选框 ===================== */
QCheckBox {{ spacing: 9px; color: {p['text']}; font-size: 13px; }}
QCheckBox::indicator {{ width: 0px; height: 0px; }}
QCheckBox:disabled {{ color: {p['text_faint']}; }}

/* ===================== 表格 ===================== */
QTableView {{
    background-color: transparent;
    border: none;
    outline: 0;
    gridline-color: transparent;
    selection-background-color: transparent;
    selection-color: {p['text']};
}}
QTableView::item {{
    border: none;
    padding: 0px;
    background: transparent;
}}
QTableView::item:selected {{ background: transparent; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{
    background-color: transparent;
    border: none;
    border-bottom: 1px solid {p['border']};
    padding: 9px 10px;
    color: {p['text_dim']};
    font-size: 12px;
    font-weight: 700;
}}
QHeaderView::section:hover {{ color: {p['text']}; }}
QHeaderView::section:first {{ padding-left: 14px; }}
QTableCornerButton::section {{ background: transparent; border: none; }}

/* ===================== 滚动条 ===================== */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px 2px 2px 0px;
}}
QScrollBar::handle:vertical {{
    background-color: {p['scroll']};
    border-radius: 4px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background-color: {p['scroll_hover']}; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0px 2px 2px 2px;
}}
QScrollBar::handle:horizontal {{
    background-color: {p['scroll']};
    border-radius: 4px;
    min-width: 32px;
}}
QScrollBar::handle:horizontal:hover {{ background-color: {p['scroll_hover']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0px; height: 0px; background: transparent; border: none;
}}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ===================== 文本域 ===================== */
QPlainTextEdit, QTextEdit {{
    background-color: {p['sunken']};
    color: {p['text']};
    border: 1px solid {p['border']};
    border-radius: 10px;
    padding: 4px 6px;
    selection-background-color: {p['accent_soft2']};
}}
QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {p['border_strong']}; }}

/* ===================== 分割器 ===================== */
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 12px; }}
QSplitter::handle:vertical {{ height: 12px; }}

/* ===================== 其它 ===================== */
QToolButton {{
    background: transparent;
    border: none;
    border-radius: 7px;
    padding: 4px;
}}
QToolButton:hover {{ background-color: {p['hover']}; }}
QToolButton:pressed {{ background-color: {p['pressed']}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
"""
