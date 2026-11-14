"""Point every user folder at a throw-away directory before any test imports Paintbooth.

Each test module imports this first (``import _isolation``). It sets ``HOME``, ``USERPROFILE``
and ``PAINTBOOTH_HOME`` to a fresh temporary directory, so running the tests never reads or
changes the real ``~/Paintbooth`` folder, the settings file or the texture cache.
"""

import atexit
import os
import shutil
import tempfile

TEMP_HOME = tempfile.mkdtemp(prefix="paintbooth-tests-")
os.environ["HOME"] = TEMP_HOME
os.environ["USERPROFILE"] = TEMP_HOME
os.environ["PAINTBOOTH_HOME"] = os.path.join(TEMP_HOME, "Paintbooth")
os.environ.pop("SKINYARD_HOME", None)
atexit.register(shutil.rmtree, TEMP_HOME, ignore_errors=True)
