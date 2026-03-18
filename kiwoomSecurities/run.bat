@echo off
chcp 65001 > nul
echo ===============================================
echo   키움증권 자동매매 시스템 시작
echo ===============================================
echo.

REM 가상환경 확인 및 활성화
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

REM 필요한 패키지 설치 확인
pip show PyQt5 > nul 2>&1
if errorlevel 1 (
    echo PyQt5 설치 중...
    pip install PyQt5
)

echo.
echo 프로그램을 시작합니다...
echo.
python main_gui.py

pause
