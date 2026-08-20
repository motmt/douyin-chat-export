@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo  Douyin Chat Export - One-time Setup
echo ============================================

set "PIP_INDEX=https://mirrors.nju.edu.cn/pypi/web/simple"

rem ---------------------------------------------------------------
rem [1/4] Locate Python: portable first, then system
rem   - runtime\python\python.exe  (portable/embedded, created by
rem     tools\00_install_deps_nju.ps1)
rem   - system python in PATH
rem ---------------------------------------------------------------
echo [1/4] Locating Python...
set "PY="
if exist "..\runtime\python\python.exe" (
    set "PY=..\runtime\python\python.exe"
    echo   Using portable Python: %PY%
) else if exist "runtime\python\python.exe" (
    set "PY=runtime\python\python.exe"
    echo   Using portable Python: %PY%
) else (
    where python >nul 2>&1
    if !errorlevel! neq 0 (
        echo   ERROR: No Python found.
        echo   Install Python 3.11+ and add to PATH, or run tools\00_install_deps_nju.ps1
        echo   to install a portable Python.
        pause
        exit /b 1
    )
    set "PY=python"
    echo   Using system Python: !PY!
)
rem Verify whichever python we picked (works with spaces in %PY%)
"%PY%" --version >nul 2>&1
if !errorlevel! neq 0 (
    echo   ERROR: Python not runnable: %PY%
    pause
    exit /b 1
)
echo   Python version OK

rem ---------------------------------------------------------------
rem [2/4] Create venv (from whichever Python we found)
rem   Portable Python: also fix the embedded ._pth so site-packages
rem   and pip work (mirrors tools\python_env.ps1 Install-PortablePython)
rem ---------------------------------------------------------------
echo [2/4] Preparing virtual environment...

rem Fix embedded Python _pth (portable only)
set "PTH_DIR=..\runtime\python"
if not exist "%PTH_DIR%" set "PTH_DIR=runtime\python"
if exist "%PTH_DIR%" (
    for %%f in ("%PTH_DIR%\python*._pth") do (
        findstr /b "import site" "%%f" >nul 2>&1
        if errorlevel 1 (
            echo   Fixing _pth: enabling site for %%f
            (echo import site) >> "%%f"
        )
    )
)

if not exist "venv" (
    if "%PY%"=="python" (
        python -m venv venv
        if !errorlevel! neq 0 (
            echo   ERROR: Failed to create venv.
            pause
            exit /b 1
        )
    ) else (
        rem Portable Python: venv module is unreliable on embed builds,
        rem so install deps into the portable Python's own site-packages
        rem (mirrors tools\00_install_deps_nju.ps1 behavior).
        echo   Portable Python detected - installing into its site-packages.
        set "VENV_PY=%PY%"
        goto deps_install
    )
    echo   venv created.
) else (
    echo   venv already exists, skipping.
)

set "VENV_PY=venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo   ERROR: venv python not found: %VENV_PY%
    pause
    exit /b 1
)

:deps_install

rem ---------------------------------------------------------------
rem [3/4] Install dependencies via NJU PyPI mirror (faster in CN)
rem ---------------------------------------------------------------
echo [3/4] Installing Python dependencies (NJU mirror)...
"%VENV_PY%" -m pip install --upgrade pip -i %PIP_INDEX%
"%VENV_PY%" -m pip install -r requirements.txt -i %PIP_INDEX%
if !errorlevel! neq 0 (
    echo   ERROR: pip install failed. Check your network and retry.
    pause
    exit /b 1
)

rem ---------------------------------------------------------------
rem [4/4] Install Playwright Chromium (browser binaries)
rem   Portable Python's playwright may need PLAYWRIGHT_BROWSERS_PATH;
rem   we default to ..\runtime\ms-playwright so browsers stay in the
rem   project (works from any directory, no %USERPROFILE% dependency).
rem ---------------------------------------------------------------
echo [4/4] Installing Playwright Chromium...
if not defined PLAYWRIGHT_BROWSERS_PATH (
    set "PLAYWRIGHT_BROWSERS_PATH=..\runtime\ms-playwright"
)
"%VENV_PY%" -m playwright install chromium
if !errorlevel! neq 0 (
    echo   WARNING: Playwright browser install failed. Run manually:
    echo     %VENV_PY% -m playwright install chromium
)

echo.
echo  ============================================
echo  Setup complete! Run start.bat to launch.
echo  Panel: http://127.0.0.1:8001/panel
echo  ============================================
echo.
pause
endlocal
