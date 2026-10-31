#!/usr/bin/env bash
# 03_butler.sh —— 管家服务：venv + 依赖 + .env 核对 + systemd 开机自启
# 用法：bash 03_butler.sh   （仓库须已在 ~/AIHomeButler，.env 已随仓库/scp 就位）
set -euo pipefail

PROJECT_DIR="$HOME/AIHomeButler"
VENV="$PROJECT_DIR/.venv"
SERVICE_NAME=butler

cd "$PROJECT_DIR"

echo "== [1/5] Python 虚拟环境（Bookworm PEP 668：必须 venv，勿 sudo pip）=="
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple

echo "== [2/5] 安装依赖（requirements 兼容 Pi 的 Python 3.11）=="
"$VENV/bin/pip" install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

echo "== [3/5] .env 核对 ="
if [ ! -f .env ]; then
  echo "错误：$PROJECT_DIR/.env 不存在。PC 上执行："
  echo "  scp D:\\AIHomeButler\\.env pi@aihome-butler.local:~/AIHomeButler/.env"
  exit 1
fi
# 只提示不代改：密钥类字段由用户自查（脚本绝不打印密钥值）
for key in LLM_API_KEY HA_TOKEN WEB_TOKEN; do
  if ! grep -qE "^${key}=..*" .env; then
    echo "警告：.env 中 ${key} 为空/缺失——HA 迁移模式应沿用 PC 值；WEB_TOKEN 可留空（启动随机生成）"
  fi
done
echo "   （COSYVOICE_URL 如需跨机调 PC，改成 http://<PC IP>:9880，见 PI_DEPLOYMENT.md §3）"

echo "== [4/5] 安装 systemd 服务（开机自启 + 崩溃自拉）=="
CURRENT_USER="$(whoami)"
sudo tee /etc/systemd/system/${SERVICE_NAME}.service >/dev/null <<EOF
[Unit]
Description=AI Home Butler (webapp)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${VENV}/bin/python -m butler.webapp --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ${SERVICE_NAME}

echo "== [5/5] 自检 ="
sleep 5
if systemctl is-active --quiet ${SERVICE_NAME}; then
  echo "   butler 服务：运行中"
else
  echo "   butler 服务未起来！查日志：journalctl -u ${SERVICE_NAME} -n 50"
  exit 1
fi
TOKEN="$(grep -E '^WEB_TOKEN=..' .env | cut -d= -f2- || true)"
if [ -n "$TOKEN" ]; then
  CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:8080/?token=${TOKEN}")
  echo "   界面 HTTP 状态：${CODE}（200=就绪）"
else
  echo "   WEB_TOKEN 未固定：本次启动令牌见 journalctl -u ${SERVICE_NAME} | grep 界面"
fi

cat <<'NOTE'

== 03_butler.sh 完成 ==
- 全量功能自检（HA 连通/设备可读）：.venv/bin/python -m butler.check_config
- 浏览器/手机访问：http://<Pi IP>:8080/?token=<WEB_TOKEN>
- ⚠️ 8080 只服务家内网，绝不端口映射公网（外网走阶段 17 隧道）
NOTE
