@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" goto :need_setup
".venv\Scripts\python.exe" -c "import tkinter, anthropic, PIL, dotenv" >nul 2>nul
if errorlevel 1 goto :need_setup
start "" ".venv\Scripts\pythonw.exe" -m chart_analyzer
exit /b 0

:need_setup
echo.
echo 아직 설치가 되어 있지 않아요.
echo 같은 폴더의 "차트분석기_설치.bat" 을 먼저 더블클릭해 주세요.
echo.
pause
exit /b 1
