. "$PSScriptRoot\config.ps1"
. "$PSScriptRoot\python_env.ps1"

Write-Host "ProjectRoot: $ProjectRoot"
Write-Host ""

if (Test-Path (Join-Path $PortableNodeDir "node.exe")) {
    $env:PATH = "$PortableNodeDir;$env:PATH"
    Write-Host "[OK] portable Node.js found: $PortableNodeDir" -ForegroundColor Green
    Write-Host ""
}

if ((Test-Path (Join-Path $PortablePythonDir "python.exe")) -or
    (Get-ChildItem -LiteralPath $PortablePythonDir -Recurse -Filter python.exe -ErrorAction SilentlyContinue | Select-Object -First 1)) {
    Write-Host "[OK] portable Python found: $PortablePythonDir" -ForegroundColor Green
    Write-Host ""
}

function Check-Cmd {
    param([string]$Name, [string]$Hint)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) {
        Write-Host "[OK] $Name -> $($cmd.Source)" -ForegroundColor Green
        & $Name --version
    } else {
        Write-Host "[MISS] $Name" -ForegroundColor Yellow
        Write-Host "       $Hint"
    }
    Write-Host ""
}

Check-Cmd "python" "Install Python 3.12 and enable Add python.exe to PATH."
try {
    $projectPython = Get-ProjectPython -ProjectRoot $ProjectRoot -UseVenv
    Write-Host "[OK] project Python -> $projectPython" -ForegroundColor Green
    Invoke-ProjectPython -Python $projectPython -Arguments @("--version")
} catch {
    Write-Host "[MISS] project Python" -ForegroundColor Yellow
    Write-Host "       $($_.Exception.Message)"
}
Write-Host ""
Check-Cmd "node" "Install Node.js 22 LTS from https://nodejs.org/."
Check-Cmd "npm" "npm comes with Node.js. Reinstall Node.js if npm is missing."

if (Test-Path (Join-Path $ProjectRoot "frontend\dist\index.html")) {
    Write-Host "[OK] frontend dist exists" -ForegroundColor Green
} else {
    Write-Host "[MISS] frontend dist" -ForegroundColor Yellow
    Write-Host "       Run 00_install_deps_nju.bat after Node.js is installed."
}
