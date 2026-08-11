@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp005_check_status.ps1"
echo.
pause

