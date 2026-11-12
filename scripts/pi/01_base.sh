#!/usr/bin/env bash
# 01_base.sh —— Pi 基础环境：系统更新 + Docker + 编译依赖 + 国内换源
# 用法：bash 01_base.sh （需 sudo 权限，会重启 docker 服务）
set -euo pipefail

echo "== [1/5] 系统更新（可能 3-10 分钟）="
sudo apt-get update -y
sudo apt-get upgrade -y

echo "== [2/5] 时区 ="
sudo timedatectl set-timezone Asia/Shanghai || true

echo "== [3/5] 基础依赖 ="
sudo apt-get install -y git curl tar libffi-dev python3-venv python3-pip python3-dev

echo "== [4/5] Docker（apt 源，国内最稳）="
if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get install -y docker.io
  sudo systemctl enable --now docker
else
  echo "Docker 已安装，跳过"
fi
sudo usermod -aG docker "$USER" || true  # 免 sudo 跑 docker（重新登录生效）

echo "== [5/5] Docker 镜像加速（ghcr 拉取 HA 镜像用）="
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.m.daocloud.io"
  ]
}
EOF
sudo systemctl restart docker

cat <<'NOTE'

== 01_base.sh 完成 ==
注意：
- usermod 的 docker 组需重新登录 SSH 后生效；本脚本内已用 sudo 兜底
- 镜像加速地址会随时间失效，拉不动时参考 docs/PI_DEPLOYMENT.md §7 更新
NOTE
