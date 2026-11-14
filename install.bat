@echo off
rem One-time setup for running Paintbooth from this source folder with your own Python.
rem Works offline: installs Pillow and numpy from the bundled wheels folder, checking every wheel
rem against the SHA-256 hashes in wheels\requirements-offline.txt. Only if that fails (no wheel
rem for your Python version, or no wheels folder) does it try to download them from the internet.
cd /d "%~dp0"
set PY=py -3
where py >nul 2>nul || set PY=python
%PY% -c "import sys; assert sys.version_info >= (3, 11)" 2>nul || (
  echo Python 3.11 or newer is needed. Install it from python.org ^(tick "Add python.exe to PATH"^).
  pause & exit /b 1
)
%PY% -c "import tkinter" 2>nul || (
  echo Your Python was installed without "tcl/tk and IDLE", which the Paintbooth window needs.
  echo Re-run the Python installer, choose Modify, and tick "tcl/tk and IDLE".
  pause & exit /b 1
)
%PY% -c "import PIL, numpy" 2>nul && (echo Pillow and numpy are already installed. & goto done)
%PY% -m pip install --no-index --find-links wheels --require-hashes -r wheels\requirements-offline.txt || (
  echo Offline install failed - trying the internet...
  %PY% -m pip install pillow numpy
)
:done
echo.
echo Setup finished. Double-click "Start Paintbooth.bat" (or Paintbooth.pyw) to open the window.
pause
