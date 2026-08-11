@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp003_build_frontend.ps1"
echo.
pause

