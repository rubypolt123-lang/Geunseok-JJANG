@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 차트 분석기 설치

echo.
echo ==================================================
echo    차트 분석기 설치를 시작합니다.
echo    인터넷이 연결되어 있어야 하고, 몇 분 걸릴 수 있어요.
echo ==================================================
echo.

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto :have_venv

echo [1/4] 파이썬을 찾는 중...
set "PY_CMD="
py -3 --version >nul 2>nul
if not errorlevel 1 set "PY_CMD=py -3"
if defined PY_CMD goto :found_python
python --version >nul 2>nul
if not errorlevel 1 set "PY_CMD=python"
if defined PY_CMD goto :found_python
goto :no_python

:found_python
%PY_CMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto :old_python
%PY_CMD% -c "import tkinter" >nul 2>nul
if errorlevel 1 goto :no_tkinter
echo       가상환경(.venv)을 만드는 중...
%PY_CMD% -m venv .venv
if errorlevel 1 goto :venv_failed
goto :install_packages

:have_venv
echo [1/4] 이미 있는 가상환경(.venv)을 사용합니다.

:install_packages
echo [2/4] 필요한 프로그램을 내려받아 설치하는 중...
"%VENV_PY%" -m pip install --disable-pip-version-check -q --upgrade pip
"%VENV_PY%" -m pip install --disable-pip-version-check -q --only-binary ":all:" -r chart_analyzer\requirements.txt
if errorlevel 1 goto :pip_failed

echo [3/4] 설치가 잘 되었는지 확인하는 중...
"%VENV_PY%" -c "import tkinter" >nul 2>nul
if errorlevel 1 goto :no_tkinter
"%VENV_PY%" -c "import anthropic, PIL, dotenv"
if errorlevel 1 goto :pip_failed

echo [4/4] 바탕화면에 바로가기를 만드는 중...
"%VENV_PY%" -m chart_analyzer shortcut

echo.
echo ==================================================
echo    설치가 끝났습니다!
echo    이제 바탕화면의 [차트 분석기] 아이콘을 더블클릭해서 실행하세요.
echo    지금 차트 분석기 창을 바로 열어 드릴게요.
echo ==================================================
echo.
start "" "%~dp0.venv\Scripts\pythonw.exe" -m chart_analyzer
pause
exit /b 0

:no_python
echo.
echo [안내] 이 컴퓨터에 파이썬이 설치되어 있지 않습니다.
echo    1. 지금 열리는 페이지에서 Python 을 내려받아 설치하세요.
echo    2. 설치 첫 화면 아래쪽의 "Add python.exe to PATH" 를 꼭 체크하세요.
echo    3. 설치가 끝나면 이 파일을 다시 더블클릭하세요.
start "" https://www.python.org/downloads/
goto :fail

:old_python
echo.
echo [안내] 파이썬 버전이 너무 오래되었습니다. 3.10 이상이 필요합니다.
echo    https://www.python.org/downloads/ 에서 최신 버전을 설치한 뒤 다시 실행하세요.
goto :fail

:no_tkinter
echo.
echo [안내] 파이썬의 창 화면 기능(tcl/tk)이 빠져 있습니다.
echo    1. 윈도우 [설정] - [앱] 에서 Python 을 찾아 [수정]을 누르세요.
echo    2. [Modify] 를 누르고 "tcl/tk and IDLE" 을 체크한 뒤 설치하세요.
echo    3. 끝나면 이 파일을 다시 더블클릭하세요.
goto :fail

:venv_failed
echo.
echo [오류] 가상환경(.venv)을 만들지 못했습니다.
echo    폴더에 쓰기 권한이 있는지 확인하고 다시 실행하세요.
goto :fail

:pip_failed
echo.
echo [오류] 필요한 프로그램을 설치하지 못했습니다.
echo    인터넷 연결을 확인하고 이 파일을 다시 더블클릭해 보세요.
echo    계속 실패하면 위에 나온 빨간 오류 내용을 캡처해서 물어봐 주세요.
goto :fail

:fail
echo.
pause
exit /b 1
