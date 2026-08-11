@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  Douyin Chat Export - One-time Setup
echo ============================================

echo [1/4] Checking Python...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: Python not found in PATH. Install Python 3.11+ first.
    pause
    exit /b 1
)
python --version

echo [2/4] Creating virtual environment...
if not exist "venv" (
    python -m venv venv
    if %errorlevel% neq 0 (
        echo  ERROR: Failed to create venv.
        pause
        exit /b 1
    )
) else (
    echo  venv already exists, skipping.
)

echo [3/4] Installing dependencies...
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo  ERROR: pip install failed. Check your network and retry.
    pause
    exit /b 1
)

echo [4/4] Installing Playwright browser (Chromium)...
venv\Scripts\python.exe -m playwright install chromium
if %errorlevel% neq 0 (
    echo  WARNING: Playwright browser install failed. Run manually:
    echo    venv\Scripts\python.exe -m playwright install chromium
)

echo.
echo  Setup complete! Run start.bat to launch the server.
echo  Panel will open at http://127.0.0.1:8001/panel
echo.
pause
endlocal
