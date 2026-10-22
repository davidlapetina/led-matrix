"""Python control for Bluetooth LE LED matrix panels that use the LED_BLE protocol."""

from .client import LedPanel, LedPanelError, discover
from .assets import Assets
from .protocol import (DataType, DeviceInfo, DiyMode, DiyMoveType, HardwareInfo,
                       SLOT_TEMPORARY, SportMode, TimerAction)
from .render import scrolling_text_gif
from .text import Emoji, HAlign, Span, TextEffect, VAlign, rainbow

__all__ = ["LedPanel", "LedPanelError", "discover", "Assets", "DataType", "DeviceInfo",
           "DiyMode", "DiyMoveType", "HardwareInfo", "SLOT_TEMPORARY", "SportMode",
           "TimerAction", "scrolling_text_gif", "Emoji", "HAlign", "Span", "TextEffect",
           "VAlign", "rainbow"]
