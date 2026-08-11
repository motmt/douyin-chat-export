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
if (-not (Test-Path $PlaywrightBrowsersDir)) {
    Write-Host "Playwright browser files are missing. Installing Chromium now..." -ForegroundColor Yellow
    Install-PlaywrightBrowsers -Python $Python -BrowsersDir $PlaywrightBrowsersDir -DownloadHost $PlaywrightDownloadHost
}
Invoke-ProjectPython -Python $Python -Arguments @("login.py")
