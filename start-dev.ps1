$ErrorActionPreference = "Stop"

$rootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $rootDir "backend"
$frontendDir = Join-Path $rootDir "frontend"
$backendPython = Join-Path $backendDir ".venv\Scripts\python.exe"
$backendActivate = Join-Path $backendDir ".venv\Scripts\Activate.ps1"
$backendEntry = Join-Path $backendDir "app\main.py"
$frontendPackage = Join-Path $frontendDir "package.json"

if (-not (Test-Path -LiteralPath $backendEntry)) {
    throw "Backend entry file not found: $backendEntry"
}

if (-not (Test-Path -LiteralPath $frontendPackage)) {
    throw "Frontend package.json not found: $frontendPackage"
}

if (-not (Test-Path -LiteralPath $backendPython)) {
    throw "Backend virtualenv python not found: $backendPython"
}

if (-not (Test-Path -LiteralPath $backendActivate)) {
    throw "Backend virtualenv activation script not found: $backendActivate"
}

$backendCommand = @"
Set-Location -LiteralPath '$backendDir'
& '$backendActivate'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 7788
"@

$frontendCommand = @"
Set-Location -LiteralPath '$frontendDir'
npm.cmd run dev
"@

Start-Process powershell.exe -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command", $backendCommand
) -WorkingDirectory $backendDir -WindowStyle Normal

Start-Process powershell.exe -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command", $frontendCommand
) -WorkingDirectory $frontendDir -WindowStyle Normal

Write-Host "Writer backend is starting at http://127.0.0.1:7788"
Write-Host "Writer frontend is starting at http://127.0.0.1:3000"

