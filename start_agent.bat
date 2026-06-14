@echo off
REM SRP agent launcher -- runs the local telemetry agent, auto-restarts on crash.
REM Optional server override:  start_agent.bat http://192.168.1.10:8000
REM Without an argument the server is read from client\config.json. Log: agent.log
title SRP Agent
cd /d "%~dp0"

set "SERVER_ARG="
if not "%~1"=="" set "SERVER_ARG=--server %~1"

:loop
echo [%date% %time%] Starting SRP agent...
python -u -m client.agent %SERVER_ARG% --log-file agent.log
set "RC=%errorlevel%"
REM exit 1/2 = config error (no server_url) -> back off longer, give a hint
if "%RC%"=="1" goto cfg
if "%RC%"=="2" goto cfg
echo [%date% %time%] Agent stopped (exit %RC%). Restarting in 5s...
timeout /t 5 /nobreak >nul
goto loop

:cfg
echo [%date% %time%] Config error: no server_url. Set it in client\config.json
echo   or run:  start_agent.bat http://YOUR-SERVER:8000      Retrying in 30s...
timeout /t 30 /nobreak >nul
goto loop
