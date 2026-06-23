$ErrorActionPreference = "Stop"

$rootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$executorDir = Join-Path $rootDir "executor"
$frontendDir = Join-Path $rootDir "frontend"
$executorPython = Join-Path $executorDir ".venv\Scripts\python.exe"
$executorActivate = Join-Path $executorDir ".venv\Scripts\Activate.ps1"
$executorEntry = Join-Path $executorDir "app\main.py"
$frontendPackage = Join-Path $frontendDir "package.json"

if (-not (Test-Path -LiteralPath $executorEntry)) {
    throw "Executor entry file not found: $executorEntry"
}

if (-not (Test-Path -LiteralPath $frontendPackage)) {
    throw "Frontend package.json not found: $frontendPackage"
}

if (-not (Test-Path -LiteralPath $executorPython)) {
    throw "Executor virtualenv python not found: $executorPython"
}

if (-not (Test-Path -LiteralPath $executorActivate)) {
    throw "Executor virtualenv activation script not found: $executorActivate"
}

$executorCommand = @"
Set-Location -LiteralPath '$executorDir'
& '$executorActivate'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 7788
"@

$frontendCommand = @"
Set-Location -LiteralPath '$frontendDir'
npm.cmd run dev
"@

Start-Process powershell.exe -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command", $executorCommand
) -WorkingDirectory $executorDir -WindowStyle Normal

Start-Process powershell.exe -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command", $frontendCommand
) -WorkingDirectory $frontendDir -WindowStyle Normal

Write-Host "Writer executor is starting at http://127.0.0.1:7788"
Write-Host "Writer frontend is starting at http://127.0.0.1:3000"

