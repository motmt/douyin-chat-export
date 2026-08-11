$ProjectRoot = Join-Path (Split-Path -Parent $PSScriptRoot) "app"
$PackageRoot = Split-Path -Parent $ProjectRoot
$PortableNodeDir = Join-Path $PackageRoot "runtime\nodejs"
$PortablePythonDir = Join-Path $PackageRoot "runtime\python"
$PlaywrightBrowsersDir = Join-Path $PackageRoot "runtime\ms-playwright"
$PlaywrightDownloadHost = "https://npmmirror.com/mirrors/playwright"
$Ports = @(8001, 8000)  # 8001 is the service port; 8000 is cleaned as legacy residue.
$PipIndexUrl = "https://mirrors.nju.edu.cn/pypi/web/simple"
$NpmRegistry = "https://repo.nju.edu.cn/repository/npm/"

