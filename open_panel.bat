@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  wishonomy panel - one click launcher
rem    find python -> start the panel -> it opens the browser ONCE
rem
rem  NOTE: no `start <url>` here on purpose.
rem        Letting cmd parse a URL can spawn a new browser window every
rem        round (happened once, 2026-10-13 - never again). The panel
rem        opens the browser itself, exactly once.
rem
rem  NOTE: this file is UTF-8. cmd reads it as GBK until `chcp 65001`
rem        runs, so every command line below is pure ASCII.
rem ============================================================

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    python --version >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    py -3 --version >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY goto :no_python

if not exist "panel.py" goto :no_panel

echo Starting wishonomy panel ... a browser window will open in a second.
echo Close the panel window (or press Ctrl+C) to stop it.
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem foreground: this window IS the panel. Ctrl+C stops it.
%PY% panel.py --host 127.0.0.1 --port 8765 --refresh --open-browser
set "RC=%ERRORLEVEL%"
echo.
echo panel exited with code %RC%
pause
exit /b %RC%

:no_python
echo Python 3 not found. Install Python 3, then double-click this file again.
pause
exit /b 1

:no_panel
echo panel.py not found next to this .bat. Keep them in the same folder.
pause
exit /b 1
