#!/usr/bin/env bash
# 02_ha.sh —— Home Assistant 容器（含 ha_data 迁移路线）
# 用法：bash 02_ha.sh
# 迁移模式：若 ~/ha_data.tar.gz 存在（PC 上按 docs/PI_DEPLOYMENT.md §2.1 打包并 scp），
#           自动解压沿用 PC 已配好的管理员/令牌/白名单——无需重新 onboarding。
set -euo pipefail

HA_DIR=/opt/homeassistant/config
IMAGE_GHCR=ghcr.io/home-assistant/home-assistant:stable
IMAGE_NJU=ghcr.nju.edu.cn/home-assistant/home-assistant:stable  # ghcr 国内代理

echo "== [1/4] 配置目录 ="
sudo mkdir -p "$HA_DIR"
sudo chown -R "$USER":"$USER" /opt/homeassistant

if [ -f "$HOME/ha_data.tar.gz" ]; then
  echo "== [2/4] 迁移模式：解压 PC 打包的 ha_data ="
  sudo tar -xzf "$HOME/ha_data.tar.gz" -C /opt/homeassistant --strip-components=1
  sudo chown -R "$USER":"$USER" "$HA_DIR"
  echo "   已沿用 PC 配置（HA_TOKEN 不变，.env 原样可用）"
else
  echo "== [2/4] 全新模式：空配置（首次启动后浏览器 onboarding 建号）=="
  echo "   注意：全新模式需在 onboarding 后重新生成长期令牌并更新 .env 的 HA_TOKEN"
fi

echo "== [3/4] 拉取 HA 镜像（arm64，约 700MB 下载）=="
if ! docker pull "$IMAGE_GHCR" 2>/dev/null; then
  echo "   ghcr.io 拉取失败，改用南大代理 $IMAGE_NJU"
  docker pull "$IMAGE_NJU"
  IMAGE_GHCR="$IMAGE_NJU"
fi

echo "== [4/4] 启动容器（host 网络：HA 直接监听 8123）=="
docker rm -f homeassistant 2>/dev/null || true
docker run -d \
  --name homeassistant \
  --restart=unless-stopped \
  --privileged \
  --network host \
  -v "$HA_DIR":/config \
  "$IMAGE_GHCR"

echo ""
echo "== 02_ha.sh 完成，HA 正在初始化（首次需 2-3 分钟）=="
echo "   验证：  curl -s -o /dev/null -w '%{http_code}' http://localhost:8123/"
echo "   迁移模式应返回 200/401；全新模式浏览器打开 http://<Pi IP>:8123 做 onboarding"
