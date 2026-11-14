"""Paintbooth desktop window (kept for compatibility: ``python -m paintbooth.app``).

The window itself lives in :mod:`paintbooth.gui.main`; ``paintbooth app`` also ends up there.
"""

from .gui.main import main

if __name__ == "__main__":
    main()
