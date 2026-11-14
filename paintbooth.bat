@echo off
rem Command line:  paintbooth new | check | build | watch | ui | scan   (run "paintbooth -h")
set PY=py -3
where py >nul 2>nul || set PY=python
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
%PY% -m paintbooth %*
