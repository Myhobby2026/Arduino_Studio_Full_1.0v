"""Tkinter / CustomTkinter layer of Arduino Studio.

The package holds every piece of the graphical shell:

``app``
    :class:`~arduino_studio.ui.app.ArduinoStudioApp` - the main window, menus,
    toolbar, status bar, view switching and all background task wiring.
``code_editor`` / ``editor_tabs`` / ``findbar`` / ``syntax``
    the editing surface (highlighting, gutter, find & replace, tab stack).
``serial_monitor`` / ``terminal_panel`` / ``widgets/console``
    the three bottom panels (serial monitor, integrated terminal, build log).
``libraries_panel`` / ``bootloader_panel`` / ``settings_view`` / ``setup_wizard``
    the full-window views plus the first-run setup screen.
``widgets``
    shared building blocks (toolbar, status bar, explorer tree, dialogs,
    themed text views, data tables).
``theme`` / ``icons``
    palette handling and the glyph labels used by buttons.

The widgets are imported lazily through :func:`__getattr__` so that
``python -m arduino_studio --version`` never has to touch Tk (and therefore
works on a machine without a display).
"""

from __future__ import annotations

from typing import Any

__all__ = ["ArduinoStudioApp", "create_app"]

_LAZY = {"ArduinoStudioApp": "app", "create_app": "app"}


def __getattr__(name: str) -> Any:  # PEP 562 module-level __getattr__
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(f"{__name__}.{module_name}")
    value = getattr(module, name)
    globals()[name] = value  # cache for subsequent lookups
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
