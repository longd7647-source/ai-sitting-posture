@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Server AI Nhan Dien Tu The Ngoi (Edge Node + Cloud Sync)
color 0A

echo ===============================================================================
echo     HE THONG THEO DOI TU THE NGOI - LOCAL SERVER (EDGE NODE)
echo ===============================================================================
echo.

:: Lay dia chi IP Wi-Fi cua may tinh
for /f "usebackq tokens=*" %%i in (`powershell.exe -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias 'Wi-Fi*').IPAddress" 2^>nul`) do (
    set LOCAL_IP=%%i
)

if "%LOCAL_IP%"=="" (
    for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4" ^| findstr "192.168."') do (
        set LOCAL_IP=%%a
    )
)
set LOCAL_IP=%LOCAL_IP: =%

echo [1] Dia chi IP Local Wi-Fi cua may tinh: %LOCAL_IP%
echo [2] Link Web Local tren may:             http://localhost:5000
echo [3] Link Cloud Render:                   https://ai-sitting-posture-1.onrender.com
echo [4] Che do:                             Edge Computing (Local 25-30 FPS)
echo                                         + Tu dong dong bo sang Cloud Render!
echo ===============================================================================
echo.
echo [*] Dang khoi dong Python MediaPipe Server... (Nhan Ctrl+C de tat)
echo [*] Trinh duyet se tu dong mo Dashboard sau 3 giay (cho server khoi dong xong)...
echo.
start /b cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:5000"

python app.py

echo.
echo Server da dung.
pause
