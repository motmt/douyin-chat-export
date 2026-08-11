$ErrorActionPreference = "Continue"
. "$PSScriptRoot\config.ps1"

function Stop-ById {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return }
    try {
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
        Write-Host "stopped pid $ProcessId"
    } catch {
        & taskkill /PID $ProcessId /F 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "taskkill pid $ProcessId"
        }
    }
}

Write-Host "Cleaning douyin-chat-export backend processes..."

if (Test-Path $ProjectRoot) {
    $projectPattern = [regex]::Escape($ProjectRoot)
    $backendPattern = "backend\.main:app|start_server\.py|uvicorn"
    $matched = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match $projectPattern -and
        $_.CommandLine -match $backendPattern
    }
    foreach ($proc in $matched) {
        Stop-ById -ProcessId $proc.ProcessId
    }
}

Start-Sleep -Seconds 1

$portPids = @()
foreach ($port in $Ports) {
    $lines = & netstat -ano | Select-String ":$port\s+.*LISTENING\s+(\d+)"
    foreach ($line in $lines) {
        if ($line.Matches.Count -gt 0) {
            $portPids += [int]$line.Matches[0].Groups[1].Value
        }
    }
}
$portPids = @($portPids | Sort-Object -Unique)

if ($portPids.Count -gt 0) {
    Write-Host "Cleaning listener pids on ports $($Ports -join ', '): $($portPids -join ', ')"
}

$children = Get-CimInstance Win32_Process | Where-Object {
    $cmd = $_.CommandLine
    $hasParentPid = $false
    foreach ($listenerPid in $portPids) {
        if ($listenerPid -gt 0 -and $cmd -match "parent_pid=$listenerPid") {
            $hasParentPid = $true
            break
        }
    }
    $_.CommandLine -and
    $_.CommandLine -match "multiprocessing\.spawn|spawn_main" -and
    (
        $portPids -contains $_.ParentProcessId -or
        $hasParentPid
    )
}

foreach ($child in $children) {
    Stop-ById -ProcessId $child.ProcessId
}

foreach ($listenerPid in $portPids) {
    Stop-ById -ProcessId $listenerPid
}

Start-Sleep -Seconds 1

foreach ($port in $Ports) {
    $remaining = & netstat -ano | Select-String ":$port\s+.*LISTENING"
    if ($remaining) {
        Write-Host "port $port still has listener:"
        $remaining | ForEach-Object { Write-Host $_.Line }
    } else {
        Write-Host "port $port is clear"
    }
}

Write-Host "Done."

