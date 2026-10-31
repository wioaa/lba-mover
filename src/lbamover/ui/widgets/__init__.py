"""widgets 子包：全部是"只画不管业务"的展示控件。

约定：**任何 widget 都不 import engine**。它们只接收契约层的数据类，
因此可以单独用假数据渲染、截图、做快照测试。
"""

from __future__ import annotations

from .cards import Badge, Card, Divider, MetricTile, hint_label, section_label
from .control_bar import ControlBar
from .flow import FlowContainer, FlowLayout
from .frag_map import FragMap
from .log_view import LogView
from .params_panel import ParamsPanel
from .progress_card import ProgressCard
from .rate_chart import RateChart
from .strategy_panel import STRATEGY_HELP, StrategyPanel
from .volume_picker import VolumePicker

__all__ = [
    "Badge",
    "Card",
    "ControlBar",
    "Divider",
    "FlowContainer",
    "FlowLayout",
    "FragMap",
    "LogView",
    "MetricTile",
    "ParamsPanel",
    "ProgressCard",
    "RateChart",
    "STRATEGY_HELP",
    "StrategyPanel",
    "VolumePicker",
    "hint_label",
    "section_label",
]
