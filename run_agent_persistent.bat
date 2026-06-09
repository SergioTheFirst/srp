@echo off
:: ────────────────────────────────────────────────────────────────────────────
:: SRP Agent — постоянный запуск с авто-рестартом
::
:: Использование:
::   run_agent_persistent.bat                         -- server_url из config.json
::   run_agent_persistent.bat http://192.168.1.10:8000 -- переопределить сервер
::
:: Лог: agent.log рядом со скриптом
:: Остановка: закрой окно или нажми Ctrl+C дважды
:: ────────────────────────────────────────────────────────────────────────────
setlocal

set "ROOT=%~dp0"
set "LOGFILE=%ROOT%agent.log"
set "SERVER_ARG="

:: Если передан аргумент — использовать как адрес сервера
if not "%~1"=="" set "SERVER_ARG=--server %~1"

:: Активация venv (ищем .venv и venv)
if exist "%ROOT%.venv\Scripts\activate.bat" (
    call "%ROOT%.venv\Scripts\activate.bat"
) else if exist "%ROOT%venv\Scripts\activate.bat" (
    call "%ROOT%venv\Scripts\activate.bat"
)

:: Проверка Python
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ОШИБКА: python не найден.
    echo  Убедись что Python установлен или venv активирован.
    echo.
    pause
    exit /b 1
)

:: Проверить что модуль агента виден
python -c "import client.agent" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ОШИБКА: модуль client.agent не найден.
    echo  Запускай bat из папки проекта: cd C:\pro\srp
    echo.
    pause
    exit /b 1
)

echo.
echo  ══════════════════════════════════════════════════
echo   SRP Agent — постоянный режим
if "%SERVER_ARG%"=="" (
    echo   Сервер : из client\config.json
) else (
    echo   Сервер : %~1
)
echo   Лог    : %LOGFILE%
echo   Стоп   : закрой окно или Ctrl+C дважды
echo  ══════════════════════════════════════════════════
echo.

cd /d "%ROOT%"

:restart
echo [%DATE% %TIME%] ── Запуск агента ──────────────────────────────────

:: Запускаем агент: вывод на экран + в лог-файл через Python
python -u -m client.agent %SERVER_ARG% --log-file "%LOGFILE%"

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [%DATE% %TIME%] Агент завершился (код: %EXIT_CODE%)
echo.

:: Если ExitCode=1 — скорее всего ошибка конфига (нет server_url), не перезапускать сразу
if %EXIT_CODE% EQU 1 (
    echo  Возможно не задан server_url в config.json.
    echo  Добавь "server_url": "http://IP:8000" в client\config.json
    echo  или передай адрес аргументом:  run_agent_persistent.bat http://IP:8000
    echo.
    echo  Повтор через 30 секунд...
    timeout /t 30 /nobreak >nul
) else (
    echo  Перезапуск через 10 секунд...
    timeout /t 10 /nobreak >nul
)

goto restart
