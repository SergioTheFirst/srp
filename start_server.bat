@echo off
REM SRP server launcher -- frees port 8000, runs uvicorn, auto-restarts on crash.
title SRP Server
cd /d "%~dp0"

:loop
echo [%date% %time%] Freeing port 8000 (if held by a stale process)...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
echo [%date% %time%] Starting SRP server...
python -m server.main
echo [%date% %time%] Server stopped (exit %errorlevel%). Restarting in 3s...
timeout /t 3 /nobreak >nul
goto loop
