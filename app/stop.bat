@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  Douyin Chat Export - Stop & Cleanup
echo ============================================

echo [1/3] Killing server on port 8001...
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue; if ($c) { $c | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }; Write-Host '  Server killed' } else { Write-Host '  No server running' }"

echo [2/3] Killing leftover Chromium processes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*browser_profile*' -and $_.Name -match 'chrom' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo [3/3] Removing pid file...
if exist ".server.pid" del ".server.pid"

echo.
echo  Done. All cleaned up.
endlocal
