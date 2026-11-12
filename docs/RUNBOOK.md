# RUNBOOK —— 运行与维护手册

更新日期：2026-10-27

## 1. 日常启动

| 方式 | 命令 | 说明 |
|---|---|---|
| 文本对话 | `python -m butler` | 命令行，最轻量 |
| Web 界面 | `python -m butler.webapp` | 浏览器打开控制台打印的带令牌地址（http://127.0.0.1:8080/?token=…） |
| 语音模式 | `python -m butler.voice`（`--mic` 语音输入） | 共用同一套确认机制 |
| 配置自检 | `python -m butler.check_config` | 任何启动问题先跑这个 |

依赖安装：`pip install -r requirements.txt`（语音可选件见 docs/VOICE.md）。

## 2. 配置自检输出解读

`python -m butler.check_config` 依次检查：配置完整性 → 模型连通 → 设备后端 →
设备健康 → 存储可写。退出码 0 为通过。API Key 永远只显示前 6 位。

## 3. Home Assistant 部署（阶段 2 收尾）

当前机器状态（2026-10-27）：Docker Desktop 已安装，**WSL2 需重启电脑完成初始化**。

1. 重启电脑；
2. 管理员 PowerShell 运行 `scripts\setup_homeassistant.ps1`
   （若 WSL 仍未装，脚本会执行 `wsl --install` 并提示再重启一次）；
3. 脚本自动启动 HA 容器（数据目录 `ha_data/`），完成后按脚本打印的 7 步接入；
4. `.env` 中设置 `DEVICE_BACKEND=homeassistant` 与 `HA_TOKEN`，
   白名单复制 `config/ha_devices.example.json` → `config/ha_devices.json`；
5. `python -m butler.check_config` 验证 → `python -m butler` 运行。

回退：`DEVICE_BACKEND=simulated` 随时可切回模拟模式；`butler.emulator` 可在没有
HA 的机器上以假乱真地调试适配器（`python -m butler.emulator --port 8123`）。

## 4. ESP32 烧录（阶段 3 收尾）

见 docs/HARDWARE.md 第 4 节。固件已通过 ESPHome 2026.10.1 校验（2025.5.2 编译产物在
`firmware/esphome/.esphome/build/`，实体名修正后 `esphome run` 会自动重新编译），
插上开发板后 `esphome run firmware/esphome/esp32-butler.yaml` 选 COM 口即可。

## 5. 日志

- 位置：`logs/butler.log`（UTF-8，1MB×3 滚动）
- 内容：工具调用（名称/参数/状态/阶段/耗时）、LLM 调用（Token 用量）、错误堆栈
- 注意：日志不含密钥；Web 界面不落对话正文，对话历史在内存中

## 6. 备份与恢复

- 备份：`powershell -File scripts\backup.ps1` → `backups/butler_backup_时间戳.zip`
  （含 .env、HA 白名单、设备状态、记忆库、固件密钥；自动保留最近 10 份）
- 恢复：解压 zip 覆盖回项目根目录对应位置，重启管家即可

## 7. 升级流程

1. `git pull`
2. `pip install -r requirements.txt`
3. `python -m pytest tests/`（离线回归必须全绿）
4. `python -m butler.check_config`
5. 重启管家进程。模拟设备状态与记忆库格式向后兼容；若不兼容，先跑 backup.ps1。

## 8. 故障排查

| 症状 | 处理 |
|---|---|
| 启动报"缺少配置 LLM_API_KEY" | 复制 `.env.example` 为 `.env` 并填写 |
| 模型服务暂时不可用 | 看日志重试记录；检查代理/网络；确认 base_url 与 key |
| HA 相关操作报 401 | HA 网页重新生成长期访问令牌，更新 .env |
| 设备一直"离线" | `check_config` 看具体设备；HA 中确认实体 state 非 unavailable |
| Web 界面打开空白 | 确认 `python -m butler.webapp` 在运行；端口被占用换 `--port` |
| 确认请求无响应 | 对话请求默认等待 120 秒，超时按拒绝处理；重新发起即可 |
| ESP32 读数 NaN/不更新 | 检查 DHT22 接线与上拉电阻；看 docs/HARDWARE.md 第 6 节 |

## 9. 安全边界（务必遵守）

- `.env`、`config/ha_devices.json`、`firmware/esphome/secrets.yaml` 均在 `.gitignore`，
  任何情况下不要提交或截图分享；
- `CONFIRMATION_MODE=never` 仅限调试，用完改回 `always`；
- Web 界面默认只绑定 127.0.0.1，**不要** `--host 0.0.0.0` 暴露到局域网/公网；
- 所有 API 都要求访问令牌：启动时控制台打印带 `?token=` 的地址（首次打开后浏览器会记住）；
  固定令牌在 .env 设 `WEB_TOKEN`
  （当前无登录鉴权）；
- 高风险设备（门锁/燃气/电热）在系统内没有注册任何工具，也不会通过 HA 白名单
  以外的路径接入；如未来需要，必须先单独设计授权流程。
