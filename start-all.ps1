$ErrorActionPreference = "Stop"

# 启动三个服务：executor(7788) + evolution(7789) + frontend(3456)。
# 与 start-dev.ps1 的区别：多起一个 evolution 服务。
# evolution 复用 executor 的 venv（依赖齐全，含第二期的 jinja2）。
# 三个服务各开一个窗口，便于分别看日志、分别 Ctrl+C。

$rootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$executorDir = Join-Path $rootDir "executor"
$frontendDir = Join-Path $rootDir "frontend"
$evolutionDir = Join-Path $rootDir "evolution"
$executorActivate = Join-Path $executorDir ".venv\Scripts\Activate.ps1"
$executorEntry = Join-Path $executorDir "app\main.py"
$frontendPackage = Join-Path $frontendDir "package.json"
$evolutionEntry = Join-Path $evolutionDir "app\main.py"
$evolutionLog = Join-Path $evolutionDir "start.log"

if (-not (Test-Path -LiteralPath $executorEntry)) { throw "Executor entry file not found: $executorEntry" }
if (-not (Test-Path -LiteralPath $frontendPackage)) { throw "Frontend package.json not found: $frontendPackage" }
if (-not (Test-Path -LiteralPath $evolutionEntry)) { throw "Evolution entry file not found: $evolutionEntry" }
if (-not (Test-Path -LiteralPath $executorActivate)) { throw "Executor venv Activate.ps1 not found: $executorActivate" }

# ── 每个服务的启动命令（激活 venv 后跑 uvicorn/npm）──
# evolution 输出重定向到 start.log：窗口若秒退，日志里能看到真实报错。
$evolutionScript = @"
Set-Location -LiteralPath '$evolutionDir'
& '$executorActivate'
python -m uvicorn app.main:app --host 127.0.0.1 --port 7789 *>&1 | Tee-Object -FilePath '$evolutionLog'
"@
$executorScript = "Set-Location -LiteralPath '$executorDir'; & '$executorActivate'; python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 7788"
$frontendScript = "Set-Location -LiteralPath '$frontendDir'; npm.cmd run dev"

# ── 启动参数：用变量先存，避免跨行 @() 解析问题 ──
$psArgs = @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command")

# 先起 evolution（执行端 complete_run 会立即 POST 到它，必须先就绪）
Start-Process -FilePath powershell.exe -ArgumentList ($psArgs + $evolutionScript) -WorkingDirectory $evolutionDir -WindowStyle Normal
Start-Sleep -Seconds 1

# 再起 executor
Start-Process -FilePath powershell.exe -ArgumentList ($psArgs + $executorScript) -WorkingDirectory $executorDir -WindowStyle Normal
Start-Sleep -Seconds 1

# 最后起 frontend
Start-Process -FilePath powershell.exe -ArgumentList ($psArgs + $frontendScript) -WorkingDirectory $frontendDir -WindowStyle Normal

Write-Host ""
Write-Host "三个服务正在启动（各开一个窗口）：" -ForegroundColor Green
Write-Host "  evolution   -> http://127.0.0.1:7789  （进化/监测面板，开发者用）"
Write-Host "  executor    -> http://127.0.0.1:7788  （写作 agent 执行端）"
Write-Host "  frontend    -> http://127.0.0.1:3456  （用户写作界面）"
Write-Host ""
Write-Host "联调观测：" -ForegroundColor Cyan
Write-Host "  跑一次前端生成后，看 evolution 窗口的 'POST /api/ingestion/notify' 日志"
Write-Host "  打开 http://127.0.0.1:7789 看 trace 自动摄入 / 标红 / LLM-judge 评分"
Write-Host ""
Write-Host "若 evolution 窗口没起来或秒退，查看 evolution/start.log" -ForegroundColor Yellow
Write-Host "前提：executor/.env 已配 EVOLUTION_NOTIFY_URL=http://127.0.0.1:7789/api/ingestion/notify" -ForegroundColor Yellow
