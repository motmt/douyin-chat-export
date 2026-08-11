@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp001_cleanup_backend.ps1"
echo.
pause

