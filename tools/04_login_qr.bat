@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp004_login_qr.ps1"
echo.
pause

