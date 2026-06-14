@echo off
REM ===================================================================
REM  Build the three SRP deploy artifacts into dist\share\ (dev box).
REM  Supersedes the old onefile build: onedir agent/tray (instant start,
REM  fewer AV false-positives, robocopy delta upgrades) + the one-command
REM  installer (tray spec section 6).
REM
REM  Layout produced (copy dist\share\ to the hidden share \\server\srp$\):
REM     setup.exe                <- the installer (UAC-admin manifest)
REM     config.template.json     <- org policy (editable on the share)
REM     VERSION                  <- idempotent-upgrade marker
REM     payload\                 <- srp-agent.exe, srp-tray.exe, DLLs, .ico,
REM                                 task_template.xml
REM ===================================================================
setlocal
cd /d "%~dp0"

python -m pip install -r requirements-build.txt || goto :err
python -m PyInstaller --clean --noconfirm packaging\srp.spec || goto :err

set SHARE=dist\share
if exist "%SHARE%" rmdir /s /q "%SHARE%"
mkdir "%SHARE%\payload" || goto :err

robocopy dist\agent "%SHARE%\payload" /E /NJH /NJS /NP >nul
robocopy dist\tray  "%SHARE%\payload" /E /NJH /NJS /NP >nul
if %ERRORLEVEL% GEQ 8 goto :err

copy /y "dist\srp-setup.exe"                 "%SHARE%\setup.exe"               >nul || goto :err
copy /y "client\deploy\config.template.json" "%SHARE%\config.template.json"    >nul || goto :err
copy /y "client\deploy\task_template.xml"    "%SHARE%\payload\task_template.xml" >nul || goto :err
copy /y "VERSION"                            "%SHARE%\VERSION"                 >nul || goto :err

echo.
echo Build complete: %SHARE%
echo Copy its contents to the hidden, IT-only share \\server\srp$\
echo Then deploy a PC with:  \\server\srp$\setup.exe --server http://IP:8000 --org 101
exit /b 0

:err
echo.
echo BUILD FAILED
exit /b 1
