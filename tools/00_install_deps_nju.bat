@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp000_install_deps_nju.ps1" %*
echo.
pause

