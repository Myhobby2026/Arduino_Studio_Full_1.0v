"""Icon/label helpers for buttons.

The app ships no binary image assets (nothing to break in a PyInstaller build).
Instead buttons use short, widely available Unicode symbols that exist in
``Segoe UI`` on Windows 10/11 and in DejaVu/Noto on Linux/macOS - no icon font
installation required, no tofu boxes.
"""

from __future__ import annotations

from typing import Optional

__all__ = ["ICONS", "icon_text", "glyph_only", "file_badge"]

#: ``name -> (symbol, fallback label)``
ICONS: dict[str, tuple[str, str]] = {
    "new_project": ("+", "New"),
    "open": ("\u2197", "Open"),
    "save": ("\u21e9", "Save"),
    "save_all": ("\u21d3", "Save all"),
    "verify": ("\u25b6", "Verify"),
    "upload": ("\u2191", "Upload"),
    "board": ("\u25a3", "Board"),
    "port": ("\u25c9", "Port"),
    "refresh": ("\u21bb", "Refresh"),
    "serial": ("\u2261", "Serial Monitor"),
    "libraries": ("\u2261", "Libraries"),
    "bootloader": ("\u26a1", "Bootloader"),
    "terminal": ("\u25aa", "Terminal"),
    "settings": ("\u2699", "Settings"),
    "close": ("\u2715", "Close"),
    "clear": ("\u2717", "Clear"),
    "copy": ("\u2398", "Copy"),
    "log": ("\u21e9", "Save log"),
    "search": ("\u2315", "Search"),
    "install": ("\u2193", "Install"),
    "uninstall": ("\u2191", "Remove"),
    "update": ("\u21c5", "Update"),
    "send": ("\u25b6", "Send"),
    "pause": ("\u23f8", "Pause"),
    "resume": ("\u25b6", "Resume"),
    "stop": ("\u25a0", "Stop"),
    "folder": ("\u25b8", "Folder"),
    "file": ("\u00b7", "File"),
    "warning": ("\u26a0", "Warning"),
    "info": ("\u2139", "Info"),
    "check": ("\u2713", "OK"),
    "error": ("\u2717", "Failed"),
    "arrow_up": ("\u2191", "Up"),
    "arrow_down": ("\u2193", "Down"),
    "zip": ("\u21e9", "ZIP"),
    "git": ("\u2387", "Git"),
    "chip": ("\u25a4", "Chip"),
    "flash": ("\u26a1", "Flash"),
    "read": ("\u21bb", "Read"),
    "backup": ("\u21e7", "Backup"),
    "help": ("?", "Help"),
    "theme": ("\u25d1", "Theme"),
    "cancel": ("\u2715", "Cancel"),
    "run": ("\u25b6", "Run"),
    "restart": ("\u21bb", "Restart"),
    "filter": ("\u2261", "Filter"),
    "wrap": ("\u21a9", "Wrap"),
    "pin": ("\u00b6", "Pin"),
    "expand": ("\u2922", "Expand"),
    "collapse": ("\u2923", "Collapse"),
    "duplicate": ("\u229e", "Duplicate"),
    "rename": ("\u270e", "Rename"),
    "delete": ("\u2716", "Delete"),
    "library_add": ("+", "Add"),
    "terminal_cmd": ("$>", "Command"),
}

#: Badge text used by the file explorer, per suffix (no icons needed).
_SUFFIX_BADGE: dict[str, str] = {
    ".ino": "INO",
    ".h": "H",
    ".hpp": "HPP",
    ".cpp": "CPP",
    ".c": "C",
    ".cc": "CC",
    ".json": "JSN",
    ".txt": "TXT",
    ".md": "MD",
    ".properties": "PRT",
    ".hex": "HEX",
    ".bin": "BIN",
}


def icon_text(name: str, with_symbol: bool = True, label_override: Optional[str] = None) -> str:
    """Compose a button label such as ``"\u25b6  Verify"``."""
    symbol, label = ICONS.get(name, ("", (name or "").replace("_", " ").title()))
    text = label_override or label
    if not with_symbol or not symbol:
        return text
    return f"{symbol}  {text}"


def glyph_only(name: str) -> Optional[str]:
    """Just the symbol for compact toolbars (``None`` when there is none)."""
    symbol, _label = ICONS.get(name, ("", ""))
    return symbol or None


def file_badge(suffix: str) -> str:
    """Short type tag shown in front of a file name in the explorer."""
    key = (suffix or "").lower()
    return _SUFFIX_BADGE.get(key, "")
