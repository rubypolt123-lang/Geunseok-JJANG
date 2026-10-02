@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 투자관리 엑셀 시세 업데이트

echo.
echo ==================================================
echo    투자관리 엑셀 - 시세 업데이트
echo    엑셀에서 투자관리.xlsx 를 열어 두었다면 먼저 닫아 주세요.
echo ==================================================
echo.

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto :have_venv

echo 처음 실행이라 준비하는 중입니다...
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
%PY_CMD% -m venv .venv
if errorlevel 1 goto :venv_failed

:have_venv
"%VENV_PY%" -c "import openpyxl" >nul 2>nul
if not errorlevel 1 goto :run_update
echo 필요한 프로그램(openpyxl)을 설치하는 중...
"%VENV_PY%" -m pip install --disable-pip-version-check -q --only-binary ":all:" -r investment_excel\requirements.txt
if errorlevel 1 goto :pip_failed

:run_update
"%VENV_PY%" -m investment_excel
if errorlevel 1 goto :fail
echo.
echo 엑셀 파일을 엽니다...
start "" "%~dp0투자관리.xlsx"
timeout /t 3 >nul
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
goto :fail

:venv_failed
echo.
echo [오류] 가상환경(.venv)을 만들지 못했습니다. 폴더에 쓰기 권한이 있는지 확인하세요.
goto :fail

:pip_failed
echo.
echo [오류] 필요한 프로그램을 설치하지 못했습니다. 인터넷 연결을 확인하고 다시 실행하세요.
goto :fail

:fail
echo.
pause
exit /b 1
