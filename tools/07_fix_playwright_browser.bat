@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp007_fix_playwright_browser.ps1"
echo.
pause

