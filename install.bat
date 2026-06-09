@echo off
setlocal enabledelayedexpansion

:: -----------------------------------------------------------------------
:: SRP Agent installer
:: Run as Administrator on the target PC alongside srp-agent.exe + config.json
:: -----------------------------------------------------------------------

net session >nul 2>&1
if errorlevel 1 (
    echo ERROR: Run as Administrator.
    pause & exit /b 1
)

set "DEST=C:\SRP"
set "EXE=srp-agent.exe"
set "TASK=SRP Agent"
set "HERE=%~dp0"

:: Verify required files are present
if not exist "%HERE%%EXE%" (
    echo ERROR: %EXE% not found next to install.bat
    pause & exit /b 1
)
if not exist "%HERE%config.json" (
    echo ERROR: config.json not found next to install.bat
    pause & exit /b 1
)

echo Installing SRP Agent to %DEST% ...

:: Stop running instance before overwriting the exe
schtasks /end /tn "%TASK%" >nul 2>&1
timeout /t 2 /nobreak >nul

:: Create install dir and copy files
if not exist "%DEST%" mkdir "%DEST%"
copy /y "%HERE%%EXE%" "%DEST%\%EXE%" >nul

:: Preserve existing config.json — it stores the device_id assigned on first run
if not exist "%DEST%\config.json" (
    copy "%HERE%config.json" "%DEST%\config.json" >nul
    echo   config.json installed.
) else (
    echo   config.json already present — keeping it (device_id preserved).
)

:: Register scheduled task via PowerShell (needed for restart-on-failure settings)
set "PS=%TEMP%\srp_task.ps1"
(
echo $a = New-ScheduledTaskAction -Execute '%DEST%\%EXE%' -Argument '--log-file %DEST%\srp-agent.log'
echo $t = New-ScheduledTaskTrigger -AtStartup
echo $s = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -StartWhenAvailable $true
echo Register-ScheduledTask -TaskName '%TASK%' -Action $a -Trigger $t -Settings $s -RunLevel Highest -User 'SYSTEM' -Force
) > "%PS%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS%"
if errorlevel 1 (
    echo ERROR: failed to register scheduled task.
    del "%PS%" >nul 2>&1
    pause & exit /b 1
)
del "%PS%" >nul 2>&1

:: Start the agent right now (don't wait for reboot)
schtasks /run /tn "%TASK%" >nul 2>&1

echo.
echo INSTALLED OK
echo   %DEST%\%EXE%
echo   %DEST%\srp-agent.log   (created on first run)
echo   Task "%TASK%"  — SYSTEM, starts on boot, restarts on crash (x3)
