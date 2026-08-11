@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp006_diagnose_startup.ps1"
echo.
pause

