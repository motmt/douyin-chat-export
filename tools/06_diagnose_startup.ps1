$ErrorActionPreference = "Continue"
. "$PSScriptRoot\config.ps1"
. "$PSScriptRoot\python_env.ps1"

Write-Host "=== douyin-chat-export startup diagnosis ==="
Write-Host "ProjectRoot: $ProjectRoot"
Write-Host "PackageRoot: $PackageRoot"
Write-Host "PlaywrightDownloadHost: $PlaywrightDownloadHost"
Write-Host ""

Write-Host "Files:"
foreach ($path in @(
    $ProjectRoot,
    (Join-Path $ProjectRoot "start_server.py"),
    (Join-Path $ProjectRoot "backend\main.py"),
    (Join-Path $ProjectRoot "requirements.txt"),
    (Join-Path $ProjectRoot "venv\Scripts\python.exe"),
    (Join-Path $PortablePythonDir "python.exe"),
    (Join-Path $PortableNodeDir "node.exe"),
    $PlaywrightBrowsersDir,
    (Join-Path $ProjectRoot "frontend\dist\index.html")
)) {
    Write-Host "  $path -> $(Test-Path $path)"
}

Write-Host ""
Write-Host "Python:"
$pushedLocation = $false
try {
    Push-Location $ProjectRoot
    $pushedLocation = $true
    $env:PLAYWRIGHT_BROWSERS_PATH = $PlaywrightBrowsersDir
    $py = Get-ProjectPython -ProjectRoot $ProjectRoot -UseVenv
    Write-Host "  selected: $py"
    Invoke-ProjectPython -Python $py -Arguments @("--version")
    Invoke-ProjectPython -Python $py -Arguments @("-c", "import sys; print(sys.executable)")
    Invoke-ProjectPython -Python $py -Arguments @("-c", "import uvicorn; print('uvicorn ok')")
    Invoke-ProjectPython -Python $py -Arguments @("-c", "import backend.main; print('backend.main ok')")
} catch {
    Write-Host "  ERROR: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    if ($pushedLocation) {
        Pop-Location
    }
}

Write-Host ""
Write-Host "Ports:"
foreach ($port in $Ports) {
    Write-Host "  Port ${port}:"
    $lines = & netstat -ano | Select-String ":$port\s+.*LISTENING"
    if ($lines) {
        $lines | ForEach-Object { Write-Host "    $($_.Line)" }
    } else {
        Write-Host "    clear"
    }
}

Write-Host ""
Write-Host "HTTP check:"
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8001/panel/api/login/check" -UseBasicParsing -TimeoutSec 5
    Write-Host "  login check: $($r.Content)"
} catch {
    Write-Host "  login check failed: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "Diagnosis done."

