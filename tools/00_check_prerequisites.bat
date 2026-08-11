@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp000_check_prerequisites.ps1"
echo.
pause

