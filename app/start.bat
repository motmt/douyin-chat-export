@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  Douyin Chat Export - Server Launcher
echo ============================================

echo [1/4] Cleaning leftover Chromium processes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*browser_profile*' -and $_.Name -match 'chrom' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

echo [2/4] Checking port 8001...
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue; if ($c) { $c | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }; Write-Host '  Port 8001 was in use - killed old server' } else { Write-Host '  Port 8001 is free' }"

if exist ".server.pid" del ".server.pid"

echo [3/4] Starting server...
start "DouyinExportServer" /min cmd /c "venv\Scripts\python.exe start_server.py > .server.log 2>&1"

echo [4/4] Waiting for server...
set /a tries=0
:waitloop
set /a tries+=1
timeout /t 1 /nobreak >nul
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if %errorlevel%==0 goto up
if %tries% geq 20 goto fail
goto waitloop

:up
echo.
echo  OK - Server is running at http://127.0.0.1:8001
echo  Panel: http://127.0.0.1:8001/panel
start "" "http://127.0.0.1:8001/panel"
goto end

:fail
echo.
echo  ERROR - Server did not start within 20s.
echo  Check .server.log for details:
type .server.log
goto end

:end
endlocal
