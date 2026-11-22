@echo off
rem Builds glass.zip (windscreen decal and door window data for every ETS2 truck) from your game.
rem Uses the Python and Paintbooth of your installed Paintbooth; nothing is installed or changed.
rem Usage: double-click, or  make_glass_bundle.bat "D:\SteamLibrary\steamapps\common\Euro Truck Simulator 2"
setlocal

set "PB="
for /f "tokens=2,*" %%a in ('reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\PaintBooth" /v InstallLocation 2^>nul ^| find "InstallLocation"') do set "PB=%%b"
if not defined PB (
  echo Paintbooth is not installed. Install it first, then run this again.
  pause
  exit /b 1
)

set "GAME=%~1"
if not defined GAME if exist "F:\SteamLibrary\steamapps\common\Euro Truck Simulator 2\def.scs" set "GAME=F:\SteamLibrary\steamapps\common\Euro Truck Simulator 2"
if not defined GAME if exist "D:\SteamLibrary\steamapps\common\Euro Truck Simulator 2\def.scs" set "GAME=D:\SteamLibrary\steamapps\common\Euro Truck Simulator 2"
if not defined GAME if exist "C:\Program Files (x86)\Steam\steamapps\common\Euro Truck Simulator 2\def.scs" set "GAME=C:\Program Files (x86)\Steam\steamapps\common\Euro Truck Simulator 2"
if not defined GAME (
  echo ETS2 was not found. Drag your "Euro Truck Simulator 2" folder onto this file.
  pause
  exit /b 1
)

echo Reading the trucks of %GAME% ...
echo This takes a few minutes.
set "PYTHONPATH=%PB%\app"
"%PB%\runtime\python.exe" "%~dp0make_glass_bundle.py" "%GAME%" "%~dp0glass.zip"
if errorlevel 1 (
  echo Something went wrong - see the messages above.
) else (
  echo Done: %~dp0glass.zip
)
pause
