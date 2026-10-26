# AI Home Butler 一键部署 Home Assistant（需要管理员权限）
# 使用时机：重启电脑完成 WSL2 安装之后，双击或在管理员 PowerShell 中执行本脚本。
# 脚本做什么：
#   1. 确保 WSL2 可用（Docker Desktop 引擎依赖）
#   2. 启动 Docker Desktop 并等待引擎就绪（最长 5 分钟）
#   3. 拉取并启动 Home Assistant 容器（数据落在 D:\AIHomeButler\ha_data）
#   4. 等待 HA 启动完成，打印后续配置指引
# 说明：本脚本可重复执行（容器已存在则直接启动）。

$ErrorActionPreference = "Stop"

Write-Host "== AI Home Butler · Home Assistant 部署脚本 ==" -ForegroundColor Cyan

# ---- 1. WSL2 检查 ----
$wslOk = $false
try {
    $status = wsl --status 2>$null
    if ($LASTEXITCODE -eq 0) { $wslOk = $true }
} catch { }
if (-not $wslOk) {
    Write-Host "[1/4] WSL 未就绪，正在安装（需要重启一次电脑后重新运行本脚本）..." -ForegroundColor Yellow
    wsl --install --no-distribution
    Write-Host "请重启电脑，然后重新以管理员身份运行本脚本。" -ForegroundColor Red
    exit 0
}
Write-Host "[1/4] WSL 已就绪"

# ---- 2. Docker 引擎 ----
$docker = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
if (-not (Test-Path $docker)) { $docker = "docker" }
& $docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[2/4] 启动 Docker Desktop..."
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    $deadline = (Get-Date).AddMinutes(5)
    while ((Get-Date) -lt $deadline) {
        & $docker info *> $null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep -Seconds 5
    }
}
& $docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[2/4] Docker 引擎未就绪。请打开 Docker Desktop 查看报错后重试。" -ForegroundColor Red
    exit 1
}
Write-Host "[2/4] Docker 引擎就绪（$(& $docker --version)）"

# ---- 3. Home Assistant 容器 ----
$dataDir = "D:\AIHomeButler\ha_data"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$existing = & $docker ps -a --filter "name=homeassistant" --format "{{.Names}}"
if ($existing -contains "homeassistant") {
    Write-Host "[3/4] 容器已存在，直接启动..."
    & $docker start homeassistant | Out-Null
} else {
    Write-Host "[3/4] 拉取并启动 Home Assistant（首次约 1-2 GB 下载）..."
    & $docker run -d --name homeassistant `
        --restart=unless-stopped `
        --privileged `
        -p 8123:8123 `
        -v "${dataDir}:/config" `
        ghcr.io/home-assistant/home-assistant:stable
}
Write-Host "[3/4] 等待 Home Assistant 完成首次启动（最长 3 分钟）..."
$deadline = (Get-Date).AddMinutes(3)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:8123/onboarding" -UseBasicParsing -TimeoutSec 5
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 5
}
if ($ready) {
    Write-Host "[4/4] Home Assistant 已就绪！" -ForegroundColor Green
} else {
    Write-Host "[4/4] HA 仍在启动中，可稍后访问 http://localhost:8123 确认。" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "后续步骤：" -ForegroundColor Cyan
Write-Host "  1. 浏览器打开 http://localhost:8123 完成用户注册（onboarding）"
Write-Host "  2. 左下角用户资料 → 安全 → 长期访问令牌 → 创建令牌"
Write-Host "  3. 把令牌写入 D:\AIHomeButler\.env 的 HA_TOKEN="
Write-Host "  4. 把 .env 中 DEVICE_BACKEND 改为 homeassistant"
Write-Host "  5. 复制 config\ha_devices.example.json 为 config\ha_devices.json 并按需修改"
Write-Host "  6. 在 D:\AIHomeButler 执行: python -m butler.check_config 验证"
Write-Host "  7. 启动管家: python -m butler（文本）或 python -m butler.webapp（网页）"
