$ErrorActionPreference = "Stop"
. "$PSScriptRoot\config.ps1"
. "$PSScriptRoot\python_env.ps1"

if (-not (Test-Path $ProjectRoot)) {
    throw "ProjectRoot not found: $ProjectRoot. Edit config.ps1 first."
}

Set-Location $ProjectRoot
$Python = Get-ProjectPython -ProjectRoot $ProjectRoot -UseVenv
if (($Python -notlike "*\venv\Scripts\python.exe") -and ($Python -notlike "$PortablePythonDir*")) {
    $Python = New-ProjectVenv -ProjectRoot $ProjectRoot -BasePython $Python
}

if (-not (Test-ProjectModule -Python $Python -ModuleName "playwright")) {
    Write-Host "Playwright Python package is missing. Installing Python dependencies..." -ForegroundColor Yellow
    Install-ProjectRequirements -ProjectRoot $ProjectRoot -Python $Python -PipIndexUrl $PipIndexUrl
}

if (Test-Path $PlaywrightBrowsersDir) {
    Write-Host "Removing old Playwright browser cache: $PlaywrightBrowsersDir"
    Remove-Item -LiteralPath $PlaywrightBrowsersDir -Recurse -Force
}

Write-Host "Installing Playwright Chromium into: $PlaywrightBrowsersDir"
Install-PlaywrightBrowsers -Python $Python -BrowsersDir $PlaywrightBrowsersDir -DownloadHost $PlaywrightDownloadHost

Write-Host ""
Write-Host "Playwright Chromium is ready." -ForegroundColor Green
