function Get-ProjectPython {
    param([string]$ProjectRoot, [switch]$UseVenv)

    if ($UseVenv) {
        $venvPython = Join-Path $ProjectRoot "venv\Scripts\python.exe"
        $venvCfg = Join-Path $ProjectRoot "venv\pyvenv.cfg"
        if ((Test-Path $venvPython) -and (Test-Path $venvCfg)) {
            return $venvPython
        }
    }

    if ($PortablePythonDir) {
        $portablePython = Join-Path $PortablePythonDir "python.exe"
        if (Test-Path $portablePython) {
            return $portablePython
        }
        if (Test-Path $PortablePythonDir) {
            $foundPortablePython = Get-ChildItem -LiteralPath $PortablePythonDir -Recurse -Filter python.exe -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($foundPortablePython) {
                return $foundPortablePython.FullName
            }
        }
    }

    $candidates = @()

    foreach ($cmd in @("py", "python")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) {
            if ($cmd -eq "py") {
                $candidates += @("py -3.12", "py -3.11", "py -3.10", "py -3")
            } else {
                $candidates += @("python")
            }
        }
    }

    foreach ($candidate in $candidates) {
        $parts = $candidate.Split(" ", 2)
        $exe = $parts[0]
        $arg = if ($parts.Count -gt 1) { $parts[1] } else { $null }
        try {
            $code = "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
            if ($arg) {
                & $exe $arg -c $code 2>$null
            } else {
                & $exe -c $code 2>$null
            }
            if ($LASTEXITCODE -eq 0) {
                return $candidate
            }
        } catch {
        }
    }

    throw "Python 3.10+ was not found. Install Python 3.12 and enable 'Add python.exe to PATH', then rerun this script."
}

function Install-PortablePython {
    param(
        [string]$PackageRoot,
        [string]$PortablePythonDir
    )

    $pythonExe = Join-Path $PortablePythonDir "python.exe"
    if (Test-Path $pythonExe) {
        $pipCheck = Invoke-NativeProcess -FilePath $pythonExe -Arguments @("-m", "pip", "--version")
        if ($pipCheck.ExitCode -ne 0) {
            $runtimeDir = Join-Path $PackageRoot "runtime"
            Install-PipForEmbeddedPython -PythonExe $pythonExe -RuntimeDir $runtimeDir
        }
        return $pythonExe
    }

    $runtimeDir = Join-Path $PackageRoot "runtime"
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

    $pythonVersion = "3.12.10"
    $zipName = "python-$pythonVersion-embed-amd64.zip"
    $zipPath = Join-Path $runtimeDir $zipName
    $downloadUrl = "https://mirrors.aliyun.com/python-release/windows/$zipName"

    if (-not (Test-Path $zipPath)) {
        Write-Host "Downloading embeddable Python $pythonVersion from Aliyun mirror..."
        Invoke-WebRequest -Uri $downloadUrl -OutFile $zipPath
    }

    if (Test-Path $PortablePythonDir) {
        Remove-Item -LiteralPath $PortablePythonDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $PortablePythonDir -Force | Out-Null

    Write-Host "Extracting portable Python to $PortablePythonDir..."
    Expand-Archive -LiteralPath $zipPath -DestinationPath $PortablePythonDir -Force

    $pth = Get-ChildItem -LiteralPath $PortablePythonDir -Filter "python*._pth" | Select-Object -First 1
    if ($pth) {
        $pthText = Get-Content -LiteralPath $pth.FullName -Raw
        $pthText = $pthText -replace "#import site", "import site"
        Set-Content -LiteralPath $pth.FullName -Value $pthText -Encoding ASCII
    }

    if (-not (Test-Path $pythonExe)) {
        throw "Embeddable Python extraction completed, but python.exe was not found at $pythonExe."
    }

    Install-PipForEmbeddedPython -PythonExe $pythonExe -RuntimeDir $runtimeDir
    return $pythonExe
}

function Install-PipForEmbeddedPython {
    param([string]$PythonExe, [string]$RuntimeDir)

    $getPip = Join-Path $RuntimeDir "get-pip.py"
    if (Test-Path $getPip) {
        Remove-Item -LiteralPath $getPip -Force
    }

    Write-Host "Downloading latest get-pip.py..."
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip

    Write-Host "Installing pip into portable Python..."
    $result = Invoke-NativeProcess -FilePath $PythonExe -Arguments @($getPip)
    if ($result.Output) { Write-Host $result.Output }
    if ($result.ExitCode -ne 0) {
        throw "get-pip.py failed with exit code $($result.ExitCode)."
    }
}

function Invoke-ProjectPython {
    param(
        [Parameter(Mandatory=$true)][string]$Python,
        [int]$TimeoutSeconds = 0,
        [Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments
    )

    $parsed = Split-PythonCommand -Python $Python
    $exe = $parsed.Exe
    $prefixArgs = $parsed.PrefixArgs
    $result = Invoke-NativeProcess -FilePath $exe -Arguments @($prefixArgs + $Arguments) -TimeoutSeconds $TimeoutSeconds
    if ($result.Output) {
        Write-Host $result.Output
    }
    $global:LASTEXITCODE = $result.ExitCode
}

function Invoke-ProjectPythonChecked {
    param(
        [Parameter(Mandatory=$true)][string]$Python,
        [int]$TimeoutSeconds = 0,
        [Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments
    )

    $parsed = Split-PythonCommand -Python $Python
    $allArgs = @($parsed.PrefixArgs + $Arguments)
    Write-Host "Command: $($parsed.Exe) $($allArgs -join ' ')"
    $exitCode = Invoke-NativeProcessLive -FilePath $parsed.Exe -Arguments $allArgs -TimeoutSeconds $TimeoutSeconds
    $global:LASTEXITCODE = $exitCode
    if ($exitCode -ne 0) {
        throw "Python command failed with exit code ${exitCode}: $Python $($Arguments -join ' ')"
    }
}

function New-ProjectVenv {
    param([string]$ProjectRoot, [string]$BasePython)

    Push-Location $ProjectRoot
    $venvPython = Join-Path $ProjectRoot "venv\Scripts\python.exe"
    $venvCfg = Join-Path $ProjectRoot "venv\pyvenv.cfg"
    if ((Test-Path (Join-Path $ProjectRoot "venv")) -and -not ((Test-Path $venvPython) -and (Test-Path $venvCfg))) {
        Write-Host "Broken venv detected. Recreating app\venv..." -ForegroundColor Yellow
        Stop-ProjectPythonProcesses -ProjectRoot $ProjectRoot
        Remove-ProjectVenv -ProjectRoot $ProjectRoot
    }
    try {
        if (-not (Test-Path $venvPython)) {
            $newName = "venv.new.$(Get-Date -Format yyyyMMddHHmmss)"
            $newPath = Join-Path $ProjectRoot $newName
            if (Test-Path $newPath) {
                Remove-Item -LiteralPath $newPath -Recurse -Force
            }
            Invoke-ProjectPythonLogged -Python $BasePython -Arguments @("-m", "venv", $newName) -ProjectRoot $ProjectRoot -LogName "create-venv.log"
            if (Test-Path (Join-Path $ProjectRoot "venv")) {
                Remove-ProjectVenv -ProjectRoot $ProjectRoot
            }
            Rename-Item -LiteralPath $newPath -NewName "venv" -ErrorAction Stop
        }
    } finally {
        Pop-Location
    }
    return $venvPython
}

function Stop-ProjectPythonProcesses {
    param([string]$ProjectRoot)

    $projectPattern = [regex]::Escape($ProjectRoot)
    $venvPattern = [regex]::Escape((Join-Path $ProjectRoot "venv"))
    $matched = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and
        ($_.CommandLine -match $projectPattern -or $_.CommandLine -match $venvPattern) -and
        ($_.Name -match "python|py")
    }

    foreach ($proc in $matched) {
        if ($proc.ProcessId -eq $PID) { continue }
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
            Write-Host "stopped pid $($proc.ProcessId)"
        } catch {
        }
    }

    Start-Sleep -Seconds 1
}

function Remove-ProjectVenv {
    param([string]$ProjectRoot)

    $venvDir = Join-Path $ProjectRoot "venv"
    if (-not (Test-Path $venvDir)) { return }

    try {
        Remove-Item -LiteralPath $venvDir -Recurse -Force -ErrorAction Stop
        return
    } catch {
        Write-Host "Could not delete app\venv directly: $($_.Exception.Message)" -ForegroundColor Yellow
    }

    $brokenName = "venv.broken.$(Get-Date -Format yyyyMMddHHmmss)"
    $brokenPath = Join-Path $ProjectRoot $brokenName
    try {
        Rename-Item -LiteralPath $venvDir -NewName $brokenName -ErrorAction Stop
        Write-Host "Renamed locked venv to $brokenPath" -ForegroundColor Yellow
    } catch {
        throw "app\venv is locked and cannot be removed or renamed. Close all project windows/processes, run 01_cleanup_backend.bat, then retry. Original error: $($_.Exception.Message)"
    }
}

function Install-ProjectRequirements {
    param(
        [string]$ProjectRoot,
        [string]$Python,
        [string]$PipIndexUrl
    )

    Push-Location $ProjectRoot
    try {
        $parsedPython = Split-PythonCommand -Python $Python
        $pipCheck = Invoke-NativeProcess -FilePath $parsedPython.Exe -Arguments @($parsedPython.PrefixArgs + @("-m", "pip", "--version"))
        if ($pipCheck.ExitCode -ne 0) {
            Install-PipForEmbeddedPython -PythonExe $parsedPython.Exe -RuntimeDir (Join-Path (Split-Path -Parent $ProjectRoot) "runtime")
        }
        Invoke-ProjectPythonChecked -Python $Python -TimeoutSeconds 180 -Arguments @(
            "-m", "pip", "install", "-U", "pip",
            "-i", $PipIndexUrl,
            "--disable-pip-version-check",
            "--timeout", "30",
            "--retries", "2"
        )

        $requirements = Get-Content -LiteralPath (Join-Path $ProjectRoot "requirements.txt") |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -and -not $_.StartsWith("#") }

        foreach ($requirement in $requirements) {
            Write-Host ""
            Write-Host "Installing Python package: $requirement" -ForegroundColor Cyan
            Invoke-ProjectPythonChecked -Python $Python -TimeoutSeconds 300 -Arguments @(
                "-m", "pip", "install", $requirement,
                "-i", $PipIndexUrl,
                "--disable-pip-version-check",
                "--timeout", "30",
                "--retries", "2",
                "--prefer-binary"
            )
        }
    } finally {
        Pop-Location
    }
}

function Install-PlaywrightBrowsers {
    param(
        [string]$Python,
        [string]$BrowsersDir,
        [string]$DownloadHost
    )

    New-Item -ItemType Directory -Path $BrowsersDir -Force | Out-Null
    $oldPath = $env:PLAYWRIGHT_BROWSERS_PATH
    $oldHost = $env:PLAYWRIGHT_DOWNLOAD_HOST
    $env:PLAYWRIGHT_BROWSERS_PATH = $BrowsersDir
    if ($DownloadHost) {
        $env:PLAYWRIGHT_DOWNLOAD_HOST = $DownloadHost
        Write-Host "PLAYWRIGHT_DOWNLOAD_HOST=$DownloadHost"
    }
    Write-Host "PLAYWRIGHT_BROWSERS_PATH=$BrowsersDir"
    try {
        $parsed = Split-PythonCommand -Python $Python
        $args = @($parsed.PrefixArgs + @("-m", "playwright", "install", "chromium"))
        Write-Host "Command: $($parsed.Exe) $($args -join ' ')"
        & $parsed.Exe @args
        if ($LASTEXITCODE -ne 0) {
            throw "Playwright Chromium install failed with exit code $LASTEXITCODE."
        }
    } finally {
        $env:PLAYWRIGHT_BROWSERS_PATH = $oldPath
        $env:PLAYWRIGHT_DOWNLOAD_HOST = $oldHost
    }
}

function Test-ProjectModule {
    param([string]$Python, [string]$ModuleName)

    Invoke-ProjectPython -Python $Python -Arguments @("-c", "import $ModuleName") 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Invoke-ProjectPythonLogged {
    param(
        [Parameter(Mandatory=$true)][string]$Python,
        [Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments,
        [string]$ProjectRoot,
        [string]$LogName = "python-command.log"
    )

    $parsed = Split-PythonCommand -Python $Python
    $exe = $parsed.Exe
    $prefixArgs = $parsed.PrefixArgs

    $logDir = Join-Path $ProjectRoot "logs"
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $logPath = Join-Path $logDir $LogName

    $allArgs = @($prefixArgs + $Arguments)
    "Command: $exe $($allArgs -join ' ')" | Set-Content -LiteralPath $logPath -Encoding UTF8
    "Time: $(Get-Date -Format s)" | Add-Content -LiteralPath $logPath -Encoding UTF8
    "" | Add-Content -LiteralPath $logPath -Encoding UTF8

    $result = Invoke-NativeProcess -FilePath $exe -Arguments $allArgs
    $exitCode = $result.ExitCode

    if ($result.Output) {
        $result.Output | Add-Content -LiteralPath $logPath -Encoding UTF8
        Write-Host $result.Output
    }

    if ($exitCode -ne 0) {
        throw "Python command failed with exit code ${exitCode}. Full log: $logPath"
    }
}

function Invoke-NativeProcess {
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [int]$TimeoutSeconds = 0
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = ($Arguments | ForEach-Object { ConvertTo-CommandLineArgument $_ }) -join " "
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = (Get-Location).Path

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    if ($TimeoutSeconds -gt 0) {
        if (-not $proc.WaitForExit($TimeoutSeconds * 1000)) {
            try { $proc.Kill() } catch {}
            return [pscustomobject]@{
                ExitCode = 124
                Output = "Timed out after $TimeoutSeconds seconds: $FilePath $($Arguments -join ' ')"
            }
        }
    } else {
        $proc.WaitForExit()
    }
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()

    $combined = @($stdout, $stderr) -join ""
    [pscustomobject]@{
        ExitCode = $proc.ExitCode
        Output = $combined.TrimEnd()
    }
}

function Invoke-NativeProcessLive {
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [int]$TimeoutSeconds = 0
    )

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = ($Arguments | ForEach-Object { ConvertTo-CommandLineArgument $_ }) -join " "
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $false
    $psi.RedirectStandardError = $false
    $psi.WorkingDirectory = (Get-Location).Path

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()

    if ($TimeoutSeconds -gt 0) {
        if (-not $proc.WaitForExit($TimeoutSeconds * 1000)) {
            try { $proc.Kill() } catch {}
            Write-Host "Timed out after $TimeoutSeconds seconds: $FilePath $($Arguments -join ' ')" -ForegroundColor Yellow
            return 124
        }
    } else {
        $proc.WaitForExit()
    }

    return $proc.ExitCode
}

function Split-PythonCommand {
    param([string]$Python)

    $clean = (@("$Python" -split "(`r`n|`n|`r)") | Where-Object { $_.Trim() } | Select-Object -Last 1).Trim()
    $parts = $clean.Split(" ", 2)
    $prefixArgs = @()
    if ($parts.Count -gt 1) {
        $prefixArgs += $parts[1]
    }
    [pscustomobject]@{
        Exe = $parts[0]
        PrefixArgs = $prefixArgs
    }
}

function ConvertTo-CommandLineArgument {
    param([string]$Argument)

    if ($null -eq $Argument) { return '""' }
    if ($Argument -notmatch '[\s"]') { return $Argument }
    $escaped = $Argument.Replace('\', '\\').Replace('"', '\"')
    return '"' + $escaped + '"'
}
