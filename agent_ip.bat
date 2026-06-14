@echo off
REM SRP agent launcher -- asks for the server address at start, then auto-restarts on crash.
title SRP Agent (custom server)
cd /d "%~dp0"

set "SRP_SERVER="
set /p "SRP_SERVER=Server address (e.g. 192.168.1.10 or http://192.168.1.10:8000): "

if "%SRP_SERVER%"=="" (
    echo No address entered. Exiting.
    pause
    exit /b 1
)

REM If no scheme given, assume http://<addr>:8000
echo %SRP_SERVER% | findstr /i "://" >nul
if errorlevel 1 set "SRP_SERVER=http://%SRP_SERVER%:8000"

echo Using server: %SRP_SERVER%

:loop
echo [%date% %time%] Starting SRP agent...
python -m client.agent --server "%SRP_SERVER%"
echo [%date% %time%] Agent stopped (exit %errorlevel%). Restarting in 5s...
timeout /t 5 /nobreak >nul
goto loop
