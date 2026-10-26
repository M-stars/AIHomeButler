# 硬件接入指南（阶段 3 · ESP32）

更新日期：2026-10-27 ｜ 方案：ESPHome（复用其 DHT 传感器组件与 HA 自动发现，不自研协议/驱动）

## 1. 通用硬件清单（最通用、最容易购买的组合，约 30-60 元）

| 物品 | 说明 | 参考价 |
|---|---|---|
| ESP32 开发板 | ESP32-WROOM-32 DevKitC（30pin 或 38pin 均可，即"ESP32开发板"） | 15-30 元 |
| DHT22 (AM2302) | 温湿度传感器；若手头是 DHT11，改配置中 `model: DHT11` 即可 | 8-20 元 |
| 杜邦线 | 母对母 ×3 | 2 元 |
| Micro-USB 数据线 | 烧录用（很多开发板附带） | — |

## 2. 接线（断电操作）

```
DHT22 模块            ESP32 DevKit
┌─────────┐
│ VCC ────┼──────────► 3V3
│ DATA ───┼──────────► GPIO4
│ GND ────┼──────────► GND
└─────────┘
```

- DHT22 **模块**（带小电路板）：自带上拉电阻，直接三根线连好即可。
- DHT22 **裸传感器**：需在 DATA 与 VCC 之间加 10kΩ 上拉电阻。
- 引脚无方向性风险低；**不要**接 5V（DHT22 3.3V 供电时读数最稳）。
- 测试执行器使用**板载 GPIO2 LED**（固件已配置为"测试灯"），安全、无外部接线。

## 3. 固件配置与编译（已完成的部分）

- 固件配置：`firmware/esphome/esp32-butler.yaml`（已通过 `esphome config` 校验）
- 密钥文件：复制 `firmware/esphome/secrets.yaml.example` 为 `secrets.yaml`，
  填入你家 WiFi 与密码；`api_encryption_key` 用示例命令生成 32 字节随机密钥。
- 编译：`esphome compile firmware/esphome/esp32-butler.yaml`
  （首次编译会下载 ESP32 工具链约 1GB，需较长时间；编译产物在 `.esphome/build/`）

## 4. 首次烧录（需要设备在手）

1. USB 连接开发板，在 Windows 设备管理器确认 COM 口号（如 `COM5`；
   若无 COM 口，安装 CP2102 或 CH340 驱动——两种芯片按板子实际而定）。
2. 执行：`esphome run firmware/esphome/esp32-butler.yaml`，选择对应 COM 口。
3. 烧录成功后设备重启并连上 WiFi。之后 OTA 无线升级，无需再插线：
   `esphome run firmware/esphome/esp32-butler.yaml` 会自动走网络更新。
4. 若 WiFi 未配置成功，设备会开放热点 `Esp32-Butler-Fallback`（密码 `butler12345`），
   连接后访问 `192.168.4.1` 配置。

## 5. 接入 Home Assistant

1. HA 网页 → 设置 → 设备与服务 → 添加集成 → **ESPHome** →
   输入设备地址 `esp32-butler.local`（或路由器里查到的 IP）。
2. 提交时粘贴 `secrets.yaml` 中的 `api_encryption_key`。
3. 成功后自动出现实体：
   - `sensor.living_room_temperature`（客厅温度）
   - `sensor.living_room_humidity`（客厅湿度）
   - `light.test_light`（测试灯）
4. 在本仓库 `config/ha_devices.json` 白名单中加入这些实体（照抄 example 格式），
   重启管家：AI 即可读取真实温湿度、控制测试灯。

## 6. 断网/异常行为（ESPHome 内建，无需自研）

- WiFi 断开：设备自动重连；期间 HA 中实体显示 `unavailable`，
  管家侧读数会正确报告"离线"（DeviceOffline 语义）。
- DHT 读失败：ESPHome 返回 NaN，HA 不更新状态——管家侧靠 `sampled_at` 新鲜度
  标记数据过期（参考 esphome/issues#397 的教训，不做假数据回填）。
- 设备重启：状态自动恢复上报，无需人工干预。

## 7. 已验证 / 待设备验证

| 项目 | 状态 |
|---|---|
| ESPHome 2025.5.2 安装（Windows, Python 3.9） | ✅ 已完成 |
| 固件配置 `esphome config` 校验 | ✅ 已完成 |
| 固件编译（下载完整工具链） | 见 git 提交记录（受网络波动影响） |
| 烧录 + 真实传感器读数 | ⬜ 需要你把开发板用 USB 接上后执行第 4 节 |
| HA 中自动发现 + AI 读取真实数据 | ⬜ 依赖上一步 |
