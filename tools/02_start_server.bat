@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp002_start_server.ps1"
echo.
pause
