@echo off
rem Opens the Paintbooth window. Drag an .ms2 project onto this file to open it directly.
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw -3 "%~dp0Paintbooth.pyw" %* & exit /b)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0Paintbooth.pyw" %* & exit /b)
echo Python was not found. Run install.bat first.
pause
