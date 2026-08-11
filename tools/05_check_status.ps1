. "$PSScriptRoot\config.ps1"

Write-Host "ProjectRoot: $ProjectRoot"
Write-Host ""

foreach ($port in $Ports) {
    Write-Host "Port $port listeners:"
    $listeners = & netstat -ano | Select-String ":$port\s+.*LISTENING"
    if ($listeners) {
        $listeners | ForEach-Object { Write-Host $_.Line }
    } else {
        Write-Host "  none"
    }
}

Write-Host ""
try {
    $login = Invoke-WebRequest -Uri "http://127.0.0.1:8001/panel/api/login/check" -UseBasicParsing -TimeoutSec 10
    Write-Host "Login check:"
    Write-Host $login.Content
} catch {
    Write-Host "Login check failed: $($_.Exception.Message)"
}

try {
    $root = Invoke-WebRequest -Uri "http://127.0.0.1:8001/" -UseBasicParsing -TimeoutSec 10
    Write-Host ""
    Write-Host "Viewer root: HTTP $($root.StatusCode), $($root.Headers['Content-Type'])"
} catch {
    Write-Host ""
    Write-Host "Viewer root failed: $($_.Exception.Message)"
}


