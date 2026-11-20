#!/usr/bin/env bash
# backup.sh —— 树莓派侧备份（scripts/backup.ps1 的 Pi 对应版）
# 打包 .env、config/、data/（记忆库/家庭台账/模拟状态）、固件密钥（如有）到 backups/。
# 用法：bash scripts/backup.sh ；恢复：把 tar.gz 解压回项目根目录即可。
set -euo pipefail

PROJECT_DIR="$HOME/AIHomeButler"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$PROJECT_DIR/backups"
TARGET="$BACKUP_DIR/butler_backup_$STAMP.tar.gz"

mkdir -p "$BACKUP_DIR"

# 逐项收集（存在才打包；不含日志与临时文件）
ITEMS=()
for rel in ".env" "config" "data" "firmware/esphome/secrets.yaml"; do
  if [ -e "$PROJECT_DIR/$rel" ]; then
    ITEMS+=("$rel")
  fi
done

if [ ${#ITEMS[@]} -eq 0 ]; then
  echo "没有可备份的文件（尚未生成任何运行数据）。"
  exit 0
fi

# 管家正在写库时打包 SQLite 有拿到 WAL 半写状态的风险：先温和提示
if systemctl is-active --quiet butler 2>/dev/null; then
  echo "提示：butler 服务正在运行，数据库处于写入状态；"
  echo "      如需绝对一致的备份，先 sudo systemctl stop butler，备份完再 start。"
fi

tar -czf "$TARGET" -C "$PROJECT_DIR" "${ITEMS[@]}"
echo "备份完成：$TARGET"

# 自动保留最近 10 份
ls -1t "$BACKUP_DIR"/butler_backup_*.tar.gz 2>/dev/null | tail -n +11 | xargs -r rm -f
echo "（自动保留最近 10 份备份）"
