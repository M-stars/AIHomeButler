# 树莓派 GPIO 硬件测试指南（pi_ceshi 分支）

更新日期：2026-11-13 ｜ 分支：`pi_ceshi` ｜ 适用：Raspberry Pi 4B（兼容 3B+/5 的 40-pin 排针）

本分支在原有「模拟 / Home Assistant」设备后端之外新增 **GPIO 直连后端**
（`DEVICE_BACKEND=gpio`）：管家进程直接用树莓派 40-pin IO 读写硬件，无需 HA。
同时把语音链路的 Windows 专用点改为跨平台实现（详见 §6 分类报告）。

## 1. 新增能力一览

| 项 | 内容 |
|---|---|
| 新后端 | `DEVICE_BACKEND=gpio`（`butler/devices/gpio.py`，gpiozero 引擎） |
| 设备类型 | light（可选 PWM 亮度）/ fan（三档 PWM）/ switch（继电器）/ sensor（DHT11/DHT22、DS18B20 1-wire、PIR 人体感应） |
| 设备清单 | `config/gpio_devices.json`（复制 `config/gpio_devices.example.json` 修改；可用 `GPIO_DEVICES_FILE` 换路径） |
| 引脚编号 | 统一 **BCM**（GPIO17 = 物理引脚 11），范围 2-27，0/1 为系统保留 |
| 安全默认 | 所有输出引脚**上电关闭**；传感器读失败如实报离线，不编造数据 |
| 安全层不变 | GPIO 控制与 HA 一样走「确认制 + 审计 + 回读验证」，门锁等高风险类结构上仍不可接入 |
| 配套脚本 | `scripts/pi/05_gpio.sh`（依赖与权限自检）、`scripts/backup.sh`（Pi 备份，对应 Windows 的 backup.ps1） |
| 语音 | `TTS_BACKEND=espeak` 为 `sapi` 的 Linux 别名；CosyVoice 播放支持 aplay/paplay/afplay/ffplay |

## 2. 硬件清单（桌面测试最小集，约 20-50 元）

| 物品 | 说明 | 参考价 |
|---|---|---|
| LED + 220Ω 电阻 | 模拟"灯"，接 GPIO17 | 1 元 |
| 继电器模块（1路，光耦） | 模拟"开关/插座"，接 GPIO23 | 5-10 元 |
| DHT22 (AM2302) 或 DHT11 | 温湿度，接 GPIO4（DHT 模块自带上拉；裸传感器需 10kΩ 上拉到 3.3V） | 8-20 元 |
| 直流风扇 + 三极管/MOS 模块 | PWM 调速，接 GPIO22 | 5-15 元 |
| PIR 人体感应（HC-SR501） | 接 GPIO27 | 5-10 元 |
| 面包板 + 杜邦线（母对母/公对母） | 连接用 | 10 元 |

DS18B20（水温/土壤温度）需在 `raspi-config` → Interface → 1-Wire 开启，接 GPIO4（与 DHT 不同时可用同一引脚，1-wire 是总线协议）。

## 3. 接线（断电操作）

```
树莓派 40-pin（引脚编号：物理/BCM）          外设
┌──────────────────────────────┐
│  1  (3.3V)  ──────────────────►  LED 电阻端 / DHT VCC / PIR VCC
│  6  (GND)   ──────────────────►  各模块 GND（共地）
│  11 (GPIO17) ─────220Ω────────►  LED 正极（LED 负极接 GND）
│  4  (GPIO4)  ─────────────────►  DHT22 DATA
│  15 (GPIO22) ─────────────────►  风扇驱动模块 PWM 入（模块电源另接 5V）
│  16 (GPIO23) ─────────────────►  继电器模块 IN（低电平触发则 active_high=false）
│  27 (GPIO27) ─────────────────►  PIR OUT
└──────────────────────────────┘
```

⚠️ 安全红线（务必遵守）：
- GPIO 只输出 **3.3V 逻辑电平**，最大拉电流约十几 mA——驱动继电器/电机/灯带
  **必须经驱动模块**，绝不从 GPIO 直接给负载供电；
- 5V 引脚（物理 2/4）与 3.3V 引脚别插混，5V 进 GPIO 会烧毁 SoC；
- 带电不插拔；首次测试只接 LED，逐台加设备。

## 4. 启用步骤

```bash
cd ~/AIHomeButler
bash scripts/pi/05_gpio.sh                                  # 依赖检查 + DHT 依赖
cp config/gpio_devices.example.json config/gpio_devices.json
nano config/gpio_devices.json                               # 按实际接线改 pin
# .env：DEVICE_BACKEND=gpio（GPIO_DEVICES_FILE 默认 config/gpio_devices.json 可不填）
sudo systemctl restart butler
```

验证（无需 API Key）：

```bash
.venv/bin/python -m butler.check_config        # 自检：逐台设备报"可读/离线"
.venv/bin/python -m butler                     # CLI 冒烟
#   你: /devices
#   你: 现在室温是多少？                        → DHT22 真实读数
#   你: 打开书桌台灯                           → 确认后 LED 亮
#   你: 把灯带亮度调到 30                       → PWM 占空比变化
#   你: 打开继电器1                            → 继电器吸合
#   你: 风扇开到低档                           → 三档调速
```

浏览器/手机访问 `http://<Pi IP>:8080/?token=<WEB_TOKEN>` 同样可控（Web 界面与
CLI 共用同一注册表，无需任何额外配置）。

## 5. 设备清单字段说明（config/gpio_devices.json）

```jsonc
{
  "devices": [
    // 灯：pwm=false 只有开/关；pwm=true 支持 set_brightness(1-100)
    { "id": "light.desk_lamp", "name": "书桌台灯", "type": "light", "area": "书房",
      "pin": 17, "active_high": true, "pwm": false, "initial_brightness": 80 },
    // 风扇：三档映射 PWM 占空比，可按硬件特性覆盖
    { "id": "fan.bench", "name": "测试风扇", "type": "fan", "area": "书房",
      "pin": 22, "speeds": { "low": 0.35, "medium": 0.65, "high": 1.0 } },
    // 继电器：低电平触发模块设 active_high=false
    { "id": "switch.relay_1", "name": "继电器1", "type": "switch", "area": "书房",
      "pin": 23, "active_high": false },
    // 传感器 kind=dht11/dht22：温度+湿度；重试 3 次失败报离线
    { "id": "sensor.room_climate", "name": "室温湿度", "type": "sensor", "area": "书房",
      "kind": "dht22", "pin": 4 },
    // 传感器 kind=ds18b20：内核 1-wire，自动扫描 28-* 设备；w1_dir 可省略
    { "id": "sensor.temp_probe", "name": "温度探针", "type": "sensor", "area": "书房",
      "kind": "ds18b20" },
    // 传感器 kind=motion：PIR
    { "id": "sensor.room_motion", "name": "人体感应", "type": "sensor", "area": "书房",
      "kind": "motion", "pin": 27 }
  ]
}
```

行为约定（与模拟/HA 后端语义一致，Agent 层零改动）：
- `set_speed` 只改档位不改变开关状态；`set_brightness` 在关灯状态只记忆档位；
- 关灯后 `read_state` 仍报告记忆的亮度（执行器回读验证依赖）；
- 所有输出引脚进程启动即关闭，重启后设备回到关闭态（GPIO 状态不持久化，安全默认）。

## 6. 全库树莓派可执行性分类报告

### A. 已改造为 Pi 可执行（本分支改动）

| 文件 | 改动 |
|---|---|
| `butler/devices/gpio.py`（新增） | GPIO 直连后端：PinFactory 抽象 + GpiozeroFactory（gpiozero 延迟导入、BadPinFactory 给修复指引） |
| `config/gpio_devices.example.json`（新增） | GPIO 设备清单示例 |
| `butler/config.py` | 新增 `BACKEND_GPIO`、`GPIO_DEVICES_FILE`（fail-fast 校验清单存在）、`TTS_BACKEND=espeak` 别名 |
| `butler/cli.py` | `_build_registry` 增加 gpio 分支 |
| `butler/voice/tts.py` | `CosyVoiceSpeaker._play_wav`：winsound → 跨平台（winsound / aplay / paplay / afplay / ffplay 降级链）；SapiSpeaker 改为跨平台描述（pyttsx3 在 Linux 自动走 espeak） |
| `butler/voice/assembly.py` | espeak 别名路由 + 降级日志文案 |
| `butler/voice/__main__.py` | 语音后端提示按平台显示（Windows SAPI5 / Linux espeak） |
| `butler/check_config.py` | gpio 后端自检（清单展示 + 非 Linux 平台警告） |
| `scripts/pi/05_gpio.sh`（新增） | GPIO 依赖/权限/配置自检脚本 |
| `scripts/backup.sh`（新增） | Pi 侧备份（对应 backup.ps1） |
| `tests/test_devices_gpio.py`（新增） | GPIO 后端 34 项单测（假引脚工厂，任意平台可跑） |
| `requirements.txt` / `.env.example` / `README.md` | GPIO/espeak 配置说明 |
| `tests/test_household_pantry.py` / `test_household_wardrobe.py` | 修复与平台无关的日期硬编码测试缺陷（TODAY 写死 2026-11-01） |

### B. 树莓派可直接运行（无需改动）

- 全部核心：`butler/`（agent/planner/webapp/cli/memory/household/tools/scheduler/
  proactive/routines）—— 纯 Python + SQLite + httpx；
- `butler/devices/`（simulated / homeassistant / ha_ws / discovery）；
- 语音识别与唤醒词：faster-whisper（Pi 4 实测 1-2 倍实时）、sherpa-onnx KWS、
  sounddevice 采集（Pi 需 `sudo apt install libportaudio2`，04_voice.sh 已含）；
- `butler/emulator/`（HA REST 模拟器，联调用）；
- `firmware/esphome/`：ESPHome 可在 Pi 安装并编译，但 ESP32 工具链首次下载约 1GB、
  编译比 PC 慢——建议固件仍在 PC 编译烧录，Pi 只负责经 HA/管家控制；
- `scripts/pi/*.sh`：本就为 Pi 编写。

### C. 不能在树莓派上运行（保留原样，按分类说明）

| 分类 | 文件 | 原因与处置 |
|---|---|---|
| **Windows 专用脚本** | `scripts/backup.ps1`、`scripts/setup_homeassistant.ps1`、`scripts/*.bat`（cosyvoice_*/wakeword_download_model）、`_local/run.cmd`、`_local/local_run.py` | PowerShell/.bat/`.venv\Scripts\python.exe` 均 Windows 专用。Pi 侧对应物：`scripts/backup.sh`（已新增）、`scripts/pi/04_voice.sh`（装 whisper/唤醒词）、`scripts/pi/03_butler.sh`（服务化） |
| **CosyVoice 语音服务端** | `scripts/cosyvoice_server.py`、`scripts/cosyvoice_setup.bat` 等 + 3GB 模型 | 设计上就留在 PC：模型 3GB+ 内存放不下，Pi 跑不动；Pi 通过 `COSYVOICE_URL=http://<PC IP>:9880` 跨机调用（见 PI_DEPLOYMENT.md §3）。本分支已让"播放合成结果"可在 Pi 完成（aplay 等） |
| **Windows SAPI5 语音合成** | pyttsx3 的 sapi5 驱动 | 仅 Windows 有 SAPI；Pi 上 pyttsx3 自动改走 espeak（`sudo apt install espeak-ng`，音质差仅兜底）。正式播报推荐 CosyVoice（PC 端合成）或保持静音模式 |
| **PC 端 HA 容器运维** | `scripts/setup_homeassistant.ps1`、`_local/ha_lab.py`、`_local/hw_probe.py` | 开发期联调工具，指向 Windows 的 Docker/模拟器；Pi 上 HA 由 `scripts/pi/02_ha.sh` 以容器部署，无需这些脚本 |
| **理论可行但不建议** | ESPHome 在 Pi 上编译固件 | 能跑但首次工具链 1GB + 编译慢（见 §B 处置建议） |

> `_local/` 目录不进 git（`.git/info/exclude` 忽略），随仓库 tar 包分发时本来就不会到达 Pi。

## 7. 验证清单（DoD，上电逐项打勾）

- [ ] `bash scripts/pi/05_gpio.sh` 全绿（gpiozero 可用、DHT 依赖装好、清单文件就位）
- [ ] `python -m butler.check_config`：GPIO 设备逐台"可读"（未接的报离线属正常）
- [ ] LED 台灯：对话"打开书桌台灯"→ 确认 → LED 亮；"关掉"→ 灭
- [ ] PWM 灯带：亮度 30/60/100 三档肉眼可辨
- [ ] 继电器：开/关有吸合声，万用表测通断正确（active_high 方向与实际一致）
- [ ] 风扇：low/medium/high 三档转速可辨
- [ ] DHT22：读数与温湿度计对照误差合理；拔掉 DATA 线后如实报离线
- [ ] PIR：挥手触发 motion_detected=true
- [ ] Web 界面与 CLI 均可控；确认制生效（未开 CONFIRMATION_MODE=never 时每步要 y/N）
- [ ] `sudo reboot` 后输出引脚仍为关闭态，服务自拉起
- [ ] （可选语音）espeak 兜底播报可用；CosyVoice 经 PC 跨机调用可用

## 8. 故障排查

| 症状 | 处置 |
|---|---|
| 启动报"GPIO 设备清单不存在" | `cp config/gpio_devices.example.json config/gpio_devices.json` 后按接线改 |
| 报"gpiozero 未安装/引脚工厂初始化失败" | `sudo apt install -y python3-gpiozero lgpio`；确认在 Pi 上而非 PC 跑 gpio 后端 |
| DHT 读数一直失败 | DHT 抖动正常，单次失败会重试 3 次；仍失败查 DATA 线/上拉电阻/供电（3.3V）；`pin` 是否 BCM4 |
| DS18B20 报"未发现" | `raspi-config` 开 1-Wire；`ls /sys/bus/w1/devices/` 应有 28-* |
| 继电器开关方向反了 | 模块是低电平触发：配置 `"active_high": false` |
| 播报无声（espeak） | `sudo apt install espeak-ng alsa-utils`；`speaker-test` 验证声卡 |
| 写引脚报"写引脚失败" | 检查是否 GPIO 直挂了重负载（必须经驱动模块）；权限（gpio 组） |
