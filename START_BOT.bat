@echo off
rem ============================================================
rem  Binance futures bot - double-click to start.
rem  First run: creates .venv (Python 3.14) and installs packages.
rem  Then opens the launcher window (bot\launcher.py).
rem ============================================================
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PY=.venv\Scripts\python.exe"

if exist "%PY%" goto packages

echo.
echo  [1/2] First run: creating the Python environment (.venv) ...
where py >nul 2>nul
if errorlevel 1 goto nopython
py -3.14 -m venv .venv
if errorlevel 1 goto nopython

:packages
"%PY%" -c "import pandas, numpy, requests, yaml, dotenv, fastapi, uvicorn" >nul 2>nul
if not errorlevel 1 goto tk
echo.
echo  [2/2] Installing packages. This takes a few minutes, please wait ...
echo.
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install --only-binary ":all:" -r requirements.txt
if errorlevel 1 goto pipfailed

:tk
"%PY%" -c "import tkinter" >nul 2>nul
if errorlevel 1 goto notk

start "" ".venv\Scripts\pythonw.exe" -m bot.launcher
exit /b 0

:nopython
echo.
echo  Python 3.14 was not found.
echo  Install it from https://www.python.org/downloads/ (check "Add python.exe to PATH"),
echo  then double-click this file again.
echo.
pause
exit /b 1

:pipfailed
echo.
echo  Package installation failed. Check your internet connection and the messages above,
echo  then double-click this file again.
echo.
pause
exit /b 1

:notk
echo.
echo  Python was installed without "tcl/tk and IDLE".
echo  Run the Python installer again, choose Modify, and enable "tcl/tk and IDLE".
echo.
pause
exit /b 1
