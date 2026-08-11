$ErrorActionPreference = "Stop"
. "$PSScriptRoot\config.ps1"

if (-not (Test-Path $ProjectRoot)) {
    throw "ProjectRoot not found: $ProjectRoot. Edit config.ps1 first."
}

if (Test-Path (Join-Path $PortableNodeDir "node.exe")) {
    $env:PATH = "$PortableNodeDir;$env:PATH"
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js not found. Run 00_install_deps_nju.bat and choose 1 first."
}

Set-Location (Join-Path $ProjectRoot "frontend")
npm install --registry=$NpmRegistry
npm run build
Write-Host ""
Write-Host "Frontend built. Restart backend to serve the viewer." -ForegroundColor Green
