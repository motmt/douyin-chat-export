$ErrorActionPreference = "Stop"
. "$PSScriptRoot\config.ps1"
. "$PSScriptRoot\python_env.ps1"

if (-not (Test-Path $ProjectRoot)) {
    throw "ProjectRoot not found: $ProjectRoot. Edit config.ps1 first."
}

Set-Location $ProjectRoot
$env:PLAYWRIGHT_BROWSERS_PATH = $PlaywrightBrowsersDir
$Python = Get-ProjectPython -ProjectRoot $ProjectRoot -UseVenv
if (($Python -notlike "*\venv\Scripts\python.exe") -and ($Python -notlike "$PortablePythonDir*")) {
    Write-Host "Project venv is missing or broken. Recreating app\venv..." -ForegroundColor Yellow
    $Python = New-ProjectVenv -ProjectRoot $ProjectRoot -BasePython $Python
}
Write-Host "ProjectRoot: $ProjectRoot"
Write-Host "Python    : $Python"
Invoke-ProjectPythonChecked -Python $Python -Arguments @("--version")

if (-not (Test-ProjectModule -Python $Python -ModuleName "uvicorn")) {
    Write-Host ""
    Write-Host "uvicorn is missing in this venv. Installing Python dependencies now..." -ForegroundColor Yellow
    Install-ProjectRequirements -ProjectRoot $ProjectRoot -Python $Python -PipIndexUrl $PipIndexUrl
}

if (-not (Test-Path $PlaywrightBrowsersDir)) {
    Write-Host ""
    Write-Host "Playwright browser files are missing. Installing Chromium now..." -ForegroundColor Yellow
    Install-PlaywrightBrowsers -Python $Python -BrowsersDir $PlaywrightBrowsersDir -DownloadHost $PlaywrightDownloadHost
}

Write-Host ""
Write-Host "Checking backend imports..."
Invoke-ProjectPythonChecked -Python $Python -Arguments @("-c", "import uvicorn, backend.main; print('backend import ok')")

Write-Host ""
$listeners = & netstat -ano | Select-String ":8001\s+.*LISTENING"
if ($listeners) {
    Write-Host "Port 8001 is already in use:" -ForegroundColor Yellow
    $listeners | ForEach-Object { Write-Host $_.Line }
    Write-Host ""
    Write-Host "Run 01_cleanup_backend.bat first, then start again." -ForegroundColor Yellow
    throw "Port 8001 is occupied."
}

if (-not (Test-Path (Join-Path $ProjectRoot "frontend\dist\index.html"))) {
    Write-Host "Warning: frontend\dist\index.html is missing." -ForegroundColor Yellow
    Write-Host "The panel can still work, but the viewer root may show Not Found."
    Write-Host "Run 03_build_frontend.bat after the server issue is fixed."
    Write-Host ""
}

Write-Host "Starting douyin-chat-export backend..."
Write-Host "Viewer: http://127.0.0.1:8001/"
Write-Host "Panel : http://127.0.0.1:8001/panel"
Write-Host ""
Invoke-ProjectPython -Python $Python -Arguments @("start_server.py")

