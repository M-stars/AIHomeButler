# 备份脚本：打包 .env、配置、模拟设备状态、记忆数据库、固件密钥（如有）
# 用法：右键"使用 PowerShell 运行"，或 powershell -File scripts\backup.ps1
# 备份不含日志与临时文件。恢复：把 zip 解压回项目根目录即可。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupDir = Join-Path $root "backups"
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$target = Join-Path $backupDir "butler_backup_$stamp.zip"

$items = @()
foreach ($rel in @(".env", "config\ha_devices.json", "data\simulated_state.json", "data\memory.db", "firmware\esphome\secrets.yaml")) {
    $path = Join-Path $root $rel
    if (Test-Path $path) { $items += $path }
}
if (-not $items) {
    Write-Host "没有可备份的文件（尚未生成任何运行数据）。" -ForegroundColor Yellow
    exit 0
}
Compress-Archive -Path $items -DestinationPath $target -Force
Write-Host "备份完成：$target" -ForegroundColor Green
Get-ChildItem $backupDir | Sort-Object LastWriteTime -Descending | Select-Object -Skip 10 | Remove-Item -ErrorAction SilentlyContinue
Write-Host "（自动保留最近 10 份备份）"
