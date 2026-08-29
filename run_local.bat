@echo off
:: ──────────────────────────────────────────────────────────────────────────
:: Локальный тест: сервер + агент на одном компе
:: Запускает два окна; закрой оба когда закончишь.
:: ──────────────────────────────────────────────────────────────────────────
setlocal

:: Activate venv if present
if exist ".venv\Scripts\activate.bat" call .venv\Scripts\activate.bat
if exist "venv\Scripts\activate.bat"  call venv\Scripts\activate.bat

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: python not found.
    pause & exit /b 1
)

:: Окно 1 — сервер (держит окно открытым)
start "SRP Server" cmd /k "python -m server.main"

:: Небольшая пауза чтобы сервер успел стартовать
timeout /t 3 /nobreak >nul

:: Окно 2 — один проход агента с явным указанием на localhost
start "SRP Agent (once)" cmd /k "python -m client.agent --once --server http://localhost:8000 --verbose"

echo.
echo Открыто два окна:
echo   SRP Server       — FastAPI на http://localhost:8000
echo   SRP Agent (once) — один проход, потом можно закрыть
echo.
echo Дашборд: http://localhost:8000
echo API:     http://localhost:8000/api/v1/scores
