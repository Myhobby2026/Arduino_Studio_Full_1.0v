"""Arduino Studio - a standalone Arduino development environment for Windows.

The package is split into three layers:

``arduino_studio.core``
    Pure-python services (project handling, Arduino CLI wrapper, serial I/O,
    library management, bootloader tooling, integrated terminal, settings).
    None of these modules import Tkinter, which keeps them unit-testable.

``arduino_studio.ui``
    CustomTkinter widgets, the code editor, panels and the main window.

``arduino_studio.examples``
    Built-in example sketches that can be generated from *File -> New Example*.
"""

from __future__ import annotations

__version__ = "1.0.0"
__app_name__ = "Arduino Studio"

__all__ = ["__version__", "__app_name__"]
