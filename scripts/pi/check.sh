#!/usr/bin/env bash
# check.sh —— 部署自检：服务状态 / HA 连通 / 令牌校验 / 设备可读 / 界面可达
# 用法：bash check.sh   （部署完成或异常排查时运行）
set -uo pipefail   # 不用 -e：逐项报告而不是首个失败即停

PROJECT_DIR="$HOME/AIHomeButler"
VENV="$PROJECT_DIR/.venv"
PASS=0; FAIL=0

ok()   { echo "  [通过] $1"; PASS=$((PASS+1)); }
bad()  { echo "  [失败] $1"; FAIL=$((FAIL+1)); }

echo "== 1/5 butler 服务 ="
systemctl is-active --quiet butler && ok "butler systemd 运行中" || bad "butler 未运行（journalctl -u butler -n 50）"

echo "== 2/5 HA 容器 ="
docker inspect -f '{{.State.Running}}' homeassistant 2>/dev/null | grep -q true \
  && ok "homeassistant 容器运行中" || bad "HA 容器未运行（docker logs homeassistant）"

echo "== 3/5 HA API 连通 + 令牌 ="
if [ -f "$PROJECT_DIR/.env" ]; then
  HA_TOKEN="$(grep -E '^HA_TOKEN=..' "$PROJECT_DIR/.env" | cut -d= -f2-)"
  HA_URL="$(grep -E '^HA_URL=..' "$PROJECT_DIR/.env" | cut -d= -f2- || echo http://localhost:8123)"
  if [ -n "${HA_TOKEN:-}" ]; then
    CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${HA_TOKEN}" \
           -m 10 "${HA_URL%/}/api/config" || echo 000)
    [ "$CODE" = "200" ] && ok "HA API 200（令牌有效）" || bad "HA API 返回 ${CODE}（000=连不上，401=令牌失效）"
  else
    bad ".env 无 HA_TOKEN"
  fi
else
  bad "找不到 .env"
fi

echo "== 4/5 设备可读（check_config）=="
if [ -x "$VENV/bin/python" ]; then
  if "$VENV/bin/python" -m butler.check_config >/tmp/butler_check.log 2>&1; then
    ok "check_config 全绿（详情 /tmp/butler_check.log）"
  else
    bad "check_config 有失败项（详情 /tmp/butler_check.log）"
  fi
else
  bad "venv 不存在（先跑 03_butler.sh）"
fi

echo "== 5/5 管家界面 ="
CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 10 http://localhost:8080/ || echo 000)
[ "$CODE" = "200" ] && ok "界面 200（http://<Pi IP>:8080）" || bad "界面返回 ${CODE}"

echo ""
echo "== 结果：${PASS} 项通过，${FAIL} 项失败 =="
[ "$FAIL" -eq 0 ]
