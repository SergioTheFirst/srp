@echo off
setlocal enabledelayedexpansion

echo === SRP — build ===
echo.

:: Activate venv if present
if exist ".venv\Scripts\activate.bat" call .venv\Scripts\activate.bat
if exist "venv\Scripts\activate.bat"  call venv\Scripts\activate.bat

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: python not found. Install Python 3.9+ on this machine or activate your venv.
    exit /b 1
)

python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    python -m pip install pyinstaller -q
    if errorlevel 1 ( echo ERROR: pip install pyinstaller failed & exit /b 1 )
)

:: ── Step 1: agent binary ────────────────────────────────────────────────────
echo [1/2] Building dist\srp-agent.exe ...
if exist build            rmdir /s /q build
if exist srp-agent.spec   del /f /q srp-agent.spec
if exist dist\srp-agent.exe del /f /q dist\srp-agent.exe

python -m PyInstaller --onefile --name srp-agent --noupx --console ^
    --hidden-import client.collectors.heartbeat ^
    --hidden-import client.collectors.historical ^
    --hidden-import client.collectors.inventory ^
    --hidden-import client.collectors.events ^
    --hidden-import client.collectors.print_jobs ^
    srp_agent_main.py
if errorlevel 1 ( echo. & echo BUILD FAILED (agent) & exit /b 1 )

:: Ensure config template exists in dist\
if not exist dist\config.json copy client\config.json dist\config.json >nul

:: ── Step 2: installer exe (embeds agent + config) ──────────────────────────
echo [2/2] Building dist\srp-setup.exe ...
if exist srp-setup.spec del /f /q srp-setup.spec
if exist dist\srp-setup.exe del /f /q dist\srp-setup.exe

python -m PyInstaller --onefile --name srp-setup --noupx --console ^
    --add-data "dist\srp-agent.exe;." ^
    --add-data "client\config.json;." ^
    installer\setup.py
if errorlevel 1 ( echo. & echo BUILD FAILED (installer) & exit /b 1 )

:: Clean up spec files
if exist srp-agent.spec  del /f /q srp-agent.spec
if exist srp-setup.spec  del /f /q srp-setup.spec

echo.
echo BUILD OK
echo.
echo   dist\srp-setup.exe   — self-contained installer (NO other files needed)
echo   dist\srp-agent.exe   — agent binary (for manual / GPO deploy)
echo   dist\config.json     — config template
echo.
echo Deploying with srp-setup.exe:
echo   srp-setup.exe --server http://192.168.x.x:8000
echo   srp-setup.exe --server http://... --token secret   ^(if ingest auth enabled^)
echo   srp-setup.exe                                      ^(interactive: prompts for URL^)
echo   srp-setup.exe --uninstall                          ^(remove from PC^)
