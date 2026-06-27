$ErrorActionPreference = "Stop"

$rootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$executorDir = Join-Path $rootDir "executor"
$frontendDir = Join-Path $rootDir "frontend"
$evolutionDir = Join-Path $rootDir "evolution"
$monitorFrontendDir = Join-Path $evolutionDir "frontend"
$executorActivate = Join-Path $executorDir ".venv\Scripts\Activate.ps1"
$executorEntry = Join-Path $executorDir "app\main.py"
$frontendPackage = Join-Path $frontendDir "package.json"
$evolutionEntry = Join-Path $evolutionDir "app\main.py"
$monitorPackage = Join-Path $monitorFrontendDir "package.json"

if (-not (Test-Path -LiteralPath $executorEntry)) { throw "Executor entry file not found: $executorEntry" }
if (-not (Test-Path -LiteralPath $frontendPackage)) { throw "Frontend package.json not found: $frontendPackage" }
if (-not (Test-Path -LiteralPath $executorActivate)) { throw "Executor venv Activate.ps1 not found: $executorActivate" }

# ── 启动命令 ──
$executorScript = @"
Set-Location -LiteralPath '$executorDir'
& '$executorActivate'
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 7788
"@

$frontendScript = @"
Set-Location -LiteralPath '$frontendDir'
npm.cmd run dev
"@

# evolution + 监测前端 dev（可选：目录存在才起）
$psArgs = @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command")

# executor
Start-Process powershell.exe -ArgumentList ($psArgs + $executorScript) -WorkingDirectory $executorDir -WindowStyle Normal
Start-Sleep -Seconds 1

# 写作前端
Start-Process powershell.exe -ArgumentList ($psArgs + $frontendScript) -WorkingDirectory $frontendDir -WindowStyle Normal

Write-Host ""
Write-Host "服务正在启动（各开一个窗口）：" -ForegroundColor Green
Write-Host "  executor    -> http://127.0.0.1:7788  （写作 agent 执行端）"
Write-Host "  frontend    -> http://127.0.0.1:3456  （写作工作区）"

# evolution + 监测前端（开发时可选）
if (Test-Path -LiteralPath $evolutionEntry) {
    $evolutionLog = Join-Path $evolutionDir "start.log"
    $evolutionScript = @"
Set-Location -LiteralPath '$evolutionDir'
& '$executorActivate'
`$env:NEXT_PUBLIC_API_BASE_URL = 'http://localhost:7789'
python -m uvicorn app.main:app --host 127.0.0.1 --port 7789 *>&1 | Tee-Object -FilePath '$evolutionLog'
"@
    Start-Process powershell.exe -ArgumentList ($psArgs + $evolutionScript) -WorkingDirectory $evolutionDir -WindowStyle Normal
    Start-Sleep -Seconds 1
    Write-Host "  evolution   -> http://127.0.0.1:7789  （进化/监测后端）"
}

if (Test-Path -LiteralPath $monitorPackage) {
    $monitorScript = @"
Set-Location -LiteralPath '$monitorFrontendDir'
`$env:NEXT_PUBLIC_API_BASE_URL = 'http://localhost:7789'
npm.cmd run dev
"@
    Start-Process powershell.exe -ArgumentList ($psArgs + $monitorScript) -WorkingDirectory $monitorFrontendDir -WindowStyle Normal
    Start-Sleep -Seconds 1
    Write-Host "  monitor     -> http://127.0.0.1:3457  （监测前端 dev）"
}

Write-Host ""
Write-Host "联调观测：" -ForegroundColor Cyan
Write-Host "  写作：http://127.0.0.1:3456（用户界面）"
Write-Host "  监测：http://127.0.0.1:3457（开发者，dev 直连 evolution 7789）"
Write-Host "  生产监测：evolution/frontend 执行 npm run build 后，http://127.0.0.1:7789 直接访问（StaticFiles 托管）"
