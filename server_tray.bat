@echo off
rem J-Space server in the system tray: one pythonw process keeps the icon and
rem the server (python -m server.main) running, and turns on autostart at
rem Windows logon (HKCU Run value "J-Space Server", no admin rights needed).
rem
rem This file MUST stay pure ASCII with CRLF line endings: cmd.exe reads a batch
rem file by byte offset (see update.bat).
rem
rem where.exe is called by FULL path and searches PATH only ($PATH:): cmd looks
rem for a program in the current directory first, and the program directory is
rem writable by regular users.
setlocal
cd /d "%~dp0"

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%PYW%" goto run
set "PYW="
for /f "delims=" %%P in ('"%SystemRoot%\System32\where.exe" $PATH:pythonw.exe 2^>nul') do if not defined PYW set "PYW=%%P"
if defined PYW goto run
echo pythonw.exe not found. Install Python 3 from python.org (with "Add to PATH")
echo or run install-server.bat first, then start server_tray.bat again.
pause
exit /b 1

:run
rem start "": the tray runs on its own, this window (and install-server.bat) does not wait.
start "" "%PYW%" "%~dp0server_tray.pyw" --install
exit /b 0
