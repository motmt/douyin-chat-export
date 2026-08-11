param(
    [ValidateSet("0", "1")]
    [string]$Mode,
    [switch]$SkipPlaywright,
    [switch]$UseVenv
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\config.ps1"
. "$PSScriptRoot\python_env.ps1"

$NodeVersion = "22.14.0"
$NodeMirrorBase = "https://npmmirror.com/mirrors/node"
$PackageRoot = Split-Path -Parent $ProjectRoot
$RuntimeDir = Join-Path $PackageRoot "runtime"
$NodeDir = Join-Path $RuntimeDir "nodejs"
$NodeZip = Join-Path $RuntimeDir "node-v$NodeVersion-win-x64.zip"
$NodeDownloadUrl = "$NodeMirrorBase/v$NodeVersion/node-v$NodeVersion-win-x64.zip"

function Step {
    param([string]$Title, [scriptblock]$Block)
    Write-Host ""
    Write-Host "==> $Title" -ForegroundColor Cyan
    & $Block
}

function Require-Project {
    if (-not (Test-Path $ProjectRoot)) {
        throw "ProjectRoot not found: $ProjectRoot. Edit tools\config.ps1 first."
    }
}

function Select-Mode {
    if ($Mode) { return $Mode }
    Write-Host ""
    Write-Host "Choose install mode:"
    Write-Host "  0 = install Python dependencies only"
    Write-Host "  1 = install portable Node.js + Python dependencies + frontend"
    Write-Host ""
    while ($true) {
        $choice = Read-Host "Press 0 or 1"
        if ($choice -in @("0", "1")) { return $choice }
        Write-Host "Invalid choice. Please press 0 or 1." -ForegroundColor Yellow
    }
}

function Get-PythonExe {
    Set-Location $ProjectRoot
    try {
        $basePython = Get-ProjectPython -ProjectRoot $ProjectRoot
    } catch {
        Step "Installing portable Python 3.12" {
            $script:basePython = Install-PortablePython -PackageRoot $PackageRoot -PortablePythonDir $PortablePythonDir
        }
        $basePython = $script:basePython
    }
    Step "Checking Python" {
        Invoke-ProjectPython -Python $basePython -Arguments @("--version")
    }

    if ($basePython -like "$PortablePythonDir*") {
        Step "Using portable Python directly" {
            $script:venvPython = $basePython
            Write-Host $script:venvPython
        }
    } else {
        Step "Creating/using project venv" {
            $script:venvPython = New-ProjectVenv -ProjectRoot $ProjectRoot -BasePython $basePython
            Write-Host $script:venvPython
        }
    }

    return $script:venvPython
}

function Install-PythonDeps {
    param([string]$Python)

    Step "Installing Python dependencies via NJU PyPI" {
        Install-ProjectRequirements -ProjectRoot $ProjectRoot -Python $Python -PipIndexUrl $PipIndexUrl
    }

    if (-not $SkipPlaywright) {
        Step "Installing Playwright Chromium" {
            Install-PlaywrightBrowsers -Python $Python -BrowsersDir $PlaywrightBrowsersDir -DownloadHost $PlaywrightDownloadHost
        }
    }
}

function Install-PortableNode {
    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null

    if (-not (Test-Path (Join-Path $NodeDir "node.exe"))) {
        Step "Downloading portable Node.js v$NodeVersion from npmmirror" {
            if (-not (Test-Path $NodeZip)) {
                Invoke-WebRequest -Uri $NodeDownloadUrl -OutFile $NodeZip
            }
        }

        Step "Extracting portable Node.js" {
            $extractDir = Join-Path $RuntimeDir "node-v$NodeVersion-win-x64"
            if (Test-Path $extractDir) {
                Remove-Item -LiteralPath $extractDir -Recurse -Force
            }
            if (Test-Path $NodeDir) {
                Remove-Item -LiteralPath $NodeDir -Recurse -Force
            }
            Expand-Archive -LiteralPath $NodeZip -DestinationPath $RuntimeDir -Force
            Rename-Item -LiteralPath $extractDir -NewName "nodejs"
        }
    }

    $env:PATH = "$NodeDir;$env:PATH"

    Step "Checking portable Node.js and npm" {
        & (Join-Path $NodeDir "node.exe") --version
        & (Join-Path $NodeDir "npm.cmd") --version
    }

    Step "Configuring npm registry to NJU mirror for this package" {
        Push-Location (Join-Path $ProjectRoot "frontend")
        try {
            & (Join-Path $NodeDir "npm.cmd") config set registry $NpmRegistry --location=project
            & (Join-Path $NodeDir "npm.cmd") config get registry
        } finally {
            Pop-Location
        }
    }
}

function Install-FrontendDeps {
    Step "Installing frontend dependencies via NJU npm registry" {
        Push-Location (Join-Path $ProjectRoot "frontend")
        try {
            npm install --registry=$NpmRegistry
        } finally {
            Pop-Location
        }
    }

    Step "Building frontend viewer" {
        Push-Location (Join-Path $ProjectRoot "frontend")
        try {
            npm run build
        } finally {
            Pop-Location
        }
    }
}

Require-Project
$selectedMode = Select-Mode
$Python = @(Get-PythonExe) | Select-Object -Last 1
$Python = "$Python".Trim()

if ($selectedMode -eq "0") {
    Install-PythonDeps -Python $Python
    Write-Host ""
    Write-Host "Done: Python dependencies installed." -ForegroundColor Green
    Write-Host "You can use the control panel backend, but the viewer needs mode 1/frontend build."
    exit 0
}

Install-PortableNode
Install-PythonDeps -Python $Python
Install-FrontendDeps

Write-Host ""
Write-Host "Done: Node.js, Python dependencies, and frontend viewer are ready." -ForegroundColor Green
Write-Host "Start with tools\02_start_server.bat"
