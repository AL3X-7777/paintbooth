"""Double-click to open Paintbooth in its own window (no console).

The installed launcher (``Paintbooth.exe``) runs this file with the bundled ``pythonw.exe``;
from a source folder, ``Start Paintbooth.bat`` runs it with the installed Python.
"""

import multiprocessing
import sys
from pathlib import Path

if __name__ == "__main__":
    multiprocessing.freeze_support()  # export workers are spawned processes
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if sys.stdout is None or sys.stderr is None:  # pythonw: keep errors in a log file
        try:
            from paintbooth import appdirs

            # Stays open for the life of the process (it *is* stdout/stderr), so no `with`.
            log = open(appdirs.ensure() / "paintbooth.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
            sys.stdout = sys.stderr = log
        except OSError:
            import io

            sys.stdout = sys.stderr = io.StringIO()
    from paintbooth.app import main

    main()
