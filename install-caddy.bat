@echo off
rem J-Space: Caddy for viewing the dashboard from the internet, in one run.
rem All logic lives in install-caddy.ps1 next to this file; started without
rem administrator rights it asks for them itself (UAC) and goes on in a new
rem window.
rem
rem This file MUST stay pure ASCII with CRLF line endings: cmd.exe reads a batch
rem file by byte offset. powershell is called by FULL path: cmd looks for a
rem program in the current directory first.
cd /d "%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-caddy.ps1"
pause
