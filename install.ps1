# install.ps1 — Caspian 一键安装器（Windows）
# 用法（PowerShell）：
#   irm https://raw.githubusercontent.com/Leeminjing/caspian-agent/main/install.ps1 | iex
# 作用：检查 Git/Python/Docker → 创建 ~/.caspian → git clone main → ~/.caspian/app
#       → 建 ~/.caspian/runtime/.venv → 装依赖 → 写最小 config/.env → 打印 setx 块
#       → 写 ~/.caspian/bin/caspian.cmd → 加入 PATH。
# 说明：https 脚本来自互联网，执行前请先审阅；更稳妥可先下载到临时目录校验哈希。

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/Leeminjing/caspian-agent.git"
$CaspHome = Join-Path $env:USERPROFILE ".caspian"
$App = Join-Path $CaspHome "app"
$Venv = Join-Path $CaspHome "runtime\.venv"
$Bin = Join-Path $CaspHome "bin"
$ConfigDir = Join-Path $CaspHome "config"
$EnvFile = Join-Path $ConfigDir ".env"

function Write-Step([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# 1) 检查前置
Write-Step "Checking prerequisites..."
$missing = @()
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { $missing += "git" }
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) { $missing += "python (or py)" }
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { $missing += "docker" }
if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host "Missing prerequisites: $($missing -join ', ')" -ForegroundColor Red
    Write-Host ""
    Write-Host "Install the missing prerequisites, then re-run the installer:" -ForegroundColor Yellow
    if ($missing -match 'git')    { Write-Host "  git:    winget install Git.Git   (or https://git-scm.com/downloads)" -ForegroundColor Yellow }
    if ($missing -match 'python') { Write-Host "  python: winget install Python.Python.3.12   (or https://www.python.org/downloads)" -ForegroundColor Yellow }
    if ($missing -match 'docker') { Write-Host "  docker: https://www.docker.com/products/docker-desktop/" -ForegroundColor Yellow }
    Write-Host ""
    Write-Host "Re-run after installing:  irm https://raw.githubusercontent.com/Leeminjing/caspian-agent/main/install.ps1 | iex" -ForegroundColor Cyan
    Read-Host "Press Enter to close" | Out-Null
    exit 1
}

# 2) 家目录
Write-Step "Creating $CaspHome"
New-Item -ItemType Directory -Force -Path $CaspHome, $ConfigDir, $Bin | Out-Null

# 3) git clone
Write-Step "Cloning $RepoUrl -> $App"
if (Test-Path (Join-Path $App ".git")) {
    Write-Host "    app already exists; pulling latest." -ForegroundColor Yellow
    git -C $App fetch origin main | Out-Host
    git -C $App reset --hard origin/main | Out-Host
} else {
    if (Test-Path $App) { Remove-Item -Recurse -Force $App }
    git clone $RepoUrl $App | Out-Host
}

# 4) venv
Write-Step "Creating virtualenv at $Venv"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    python -m venv $Venv
}
$Python = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host ""
    Write-Host "创建 Python 虚拟环境失败：$Python 不存在。" -ForegroundColor Red
    Write-Host "这通常是 python 指向了 Microsoft Store 的占位别名（并不是真正的 Python）。" -ForegroundColor Yellow
    Write-Host "请先安装真正的 Python 3.11+，然后关闭占位别名并重开终端：" -ForegroundColor Yellow
    Write-Host "  winget install Python.Python.3.12   或 https://www.python.org/downloads" -ForegroundColor Yellow
    Write-Host "  设置 > 管理应用执行别名 → 关闭 'python.exe' / 'python3.exe'" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Press Enter to close" | Out-Null
    exit 1
}
# python >= 3.11（harness 需要）
$pyVer = (& $Python -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
$pyOk = $false
try { $pyOk = ([version]$pyVer -ge [version]"3.11") } catch { $pyOk = $false }
if (-not $pyOk) {
    Write-Host "python 版本过低：$pyVer（需要 >= 3.11）。请安装 Python 3.11+ 后重试（winget install Python.Python.3.12）。" -ForegroundColor Yellow
    Read-Host "Press Enter to close" | Out-Null
    exit 1
}

# 5) 依赖
Write-Step "Installing Python dependencies ([runtime,postgres])..."
& $Python -m pip install --upgrade pip | Out-Host
$harness = Join-Path $App "backend\packages\harness"
& $Python -m pip install -e "$harness[runtime,postgres]" | Out-Host

# 6) 最小 config/.env（随机 JWT_SECRET，不覆盖已存在值）
Write-Step "Writing minimal config/.env (JWT_SECRET only; API keys via setx)"
$lines = @()
if (Test-Path $EnvFile) { $lines = @(Get-Content $EnvFile -ErrorAction SilentlyContinue | Where-Object { $_ -notmatch '^\s*JWT_SECRET=' }) }
$jwt = if ($env:JWT_SECRET) { $env:JWT_SECRET } else { -join ((48..57 + 97..122) | Get-Random -Count 64 | ForEach-Object { [char]$_ }) }
$lines += "JWT_SECRET=$jwt"
Set-Content -Path $EnvFile -Value $lines -Encoding UTF8

# 7) PATH shim
Write-Step "Writing $Bin\caspian.cmd"
$shim = "@echo off`r`n`"%~dp0..\runtime\.venv\Scripts\python.exe`" -m caspian.cli %*`r`n"
Set-Content -Path (Join-Path $Bin "caspian.cmd") -Value $shim -Encoding ASCII

# 8) 加入用户 PATH（幂等）
Write-Step "Adding $Bin to user PATH"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$Bin*") {
    $newUserPath = if ($userPath) { "$userPath;$Bin" } else { $Bin }
    [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
    Write-Host "    Added to PATH. Reopen your terminal to use 'caspian'." -ForegroundColor Yellow
}

# 9) 打印 setx 块
Write-Step "Next: set your API keys with setx (then open a NEW terminal and run 'caspian')"
Write-Host 'setx OPENAI_API_KEY      "<your DeepSeek key>"'
Write-Host 'setx DASHSCOPE_API_KEY   "<your DashScope key>"'
Write-Host 'setx OPENAI_BASE_URL     "https://api.deepseek.com"   # optional'
Write-Host 'setx OPENAI_MODEL        "deepseek-v4-flash-vision-exp"   # optional'
Write-Host ''

Write-Host "Caspian installed successfully. Run:" -ForegroundColor Green
Write-Host "    caspian" -ForegroundColor Green
