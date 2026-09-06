#!/usr/bin/env python3
"""Start Arduino Studio from a source checkout.

``python run.py`` is equivalent to ``python -m arduino_studio`` - it exists so a
double-click in Explorer works without installing anything, and so the app can
be launched from a checkout that was never ``pip install``-ed.  Every argument
is forwarded untouched::

    python run.py --help
    python run.py C:\\Users\\you\\Documents\\Blink
    python run.py --new-project SensorNode
    python run.py --check          # headless self test, no window

The only extra thing this launcher does is fail *early and loudly* when the Tk
toolkit is missing, because that error is otherwise an import traceback.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: switches that must work even on a machine without a GUI toolkit
HEADLESS_FLAGS = ("--help", "-h", "--version", "-V", "--check")


def _tk_available() -> bool:
    """Return False (and explain) when Tkinter cannot be imported."""
    try:
        import tkinter  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on the machine
        print(f"Tkinter is not available: {exc}", file=sys.stderr)
        print(
            "Install it:\n"
            "  Windows / macOS  - use the python.org installer (Tkinter is included)\n"
            "  Debian / Ubuntu  - sudo apt install python3-tk\n"
            "  Fedora           - sudo dnf install python3-tkinter",
            file=sys.stderr,
        )
        return False
    return True


def main(argv: Optional[List[str]] = None) -> int:
    """Run the GUI, or forward ``--help`` / ``--check`` without opening a window."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not any(flag in arguments for flag in HEADLESS_FLAGS) and not _tk_available():
        return 3
    from arduino_studio.main import main as app_main

    return int(app_main(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
