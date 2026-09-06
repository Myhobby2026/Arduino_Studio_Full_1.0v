"""``python -m arduino_studio`` - start the IDE from the command line.

All the work happens in :func:`arduino_studio.main.main`; this module only
exists so the package can be executed directly, which is the fastest way to
try the app from a source checkout::

    python -m arduino_studio               # open the last project
    python -m arduino_studio --help        # every switch
    python -m arduino_studio --check       # headless support report
"""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
