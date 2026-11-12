#!/usr/bin/env bash
# 05_gpio.sh —— 可选：树莓派 GPIO 直连硬件（DEVICE_BACKEND=gpio）
# gpiozero/lgpio 在 Raspberry Pi OS 已预装；本脚本做环境检查 + DHT 依赖 + 配置指引。
# 用法：bash 05_gpio.sh   （在 03_butler.sh 之后执行；不用 GPIO 直连可跳过）
set -euo pipefail

PROJECT_DIR="$HOME/AIHomeButler"
VENV="$PROJECT_DIR/.venv"
PIP_I="-i https://pypi.tuna.tsinghua.edu.cn/simple"

echo "== [1/4] gpiozero 可用性检查（Raspberry Pi OS 预装）=="
if "$VENV/bin/python" -c "import gpiozero" 2>/dev/null; then
  echo "   gpiozero 可用"
else
  echo "   gpiozero 缺失，尝试安装……"
  sudo apt-get install -y python3-gpiozero lgpio \
    || "$VENV/bin/pip" install gpiozero lgpio $PIP_I
fi

echo "== [2/4] DHT 温湿度依赖（用 DHT11/DHT22 才需要；不用可跳过）=="
sudo apt-get install -y libgpiod2 || echo "提示：libgpiod2 安装失败，DHT 可能不可用"
"$VENV/bin/pip" install adafruit-circuitpython-dht $PIP_I \
  || echo "警告：adafruit-circuitpython-dht 安装失败（不用 DHT 可忽略）"

echo "== [3/4] GPIO 权限检查 =="
if id -nG "$USER" | grep -qw gpio; then
  echo "   用户已在 gpio 组"
else
  echo "   提示：执行 sudo usermod -aG gpio $USER 并重新登录（Bookworm 默认用户通常已具备）"
fi

echo "== [4/4] 设备清单与 .env =="
if [ -f "$PROJECT_DIR/config/gpio_devices.json" ]; then
  echo "   config/gpio_devices.json 已就位"
else
  echo "   尚无设备清单。复制示例并按实际接线修改："
  echo "     cp $PROJECT_DIR/config/gpio_devices.example.json $PROJECT_DIR/config/gpio_devices.json"
  echo "   接线表与安全须知见 docs/PI_GPIO_TESTING.md"
fi
grep -qE '^DEVICE_BACKEND=gpio' "$PROJECT_DIR/.env" 2>/dev/null \
  && echo "   .env 已启用 DEVICE_BACKEND=gpio" \
  || echo "   提示：接线配置好后，把 .env 的 DEVICE_BACKEND 改为 gpio 并 sudo systemctl restart butler"

cat <<'NOTE'

== 05_gpio.sh 完成 ==
- 冒烟验证（无需 API Key，逐台读设备）：
    .venv/bin/python -m butler.check_config
- GPIO 后端自检会逐台报告"可读/离线"——接线正确的设备应显示可读
- ⚠️ 安全红线：继电器/电机务必经驱动模块连接，GPIO 只输出 3.3V 逻辑电平；
  带电不插拔接线；首次测试建议只接 LED
NOTE
