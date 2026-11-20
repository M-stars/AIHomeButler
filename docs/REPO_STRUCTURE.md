# 本地测试库总览（结构分类与功能对照）

> 生成日期：2026-11-15 ｜ 对应分支：`pi_ceshi`（GPIO 直连硬件改造，已推送远端）

## 仓库位置

- **本地**：`D:\wendang\xiangmuwenjianku\zhinengzhushou`
- **远端**：https://github.com/M-stars/AIHomeButler.git
- **当前分支**：`pi_ceshi`（已推送；本地 `main` 与远端 `main` 均未动）
- **Python 环境**：`.venv\`（Windows 侧用 `.venv\Scripts\python.exe`；Pi 上重建为 `.venv/bin/`）
- **Pi 上测试入口**：`scripts/pi/05_gpio.sh` → 配 `config/gpio_devices.json` 与 `.env`
  （`DEVICE_BACKEND=gpio`）→ `python -m butler.check_config` → CLI/Web 控制硬件

## 1️⃣ 主程序包 `butler/` —— 管家本体（Python）

| 分类 | 文件 | 功能 |
|---|---|---|
| **入口层** | `__main__.py` → `cli.py` | 文本 CLI 交互（多轮对话、`/devices`、`/task`），UTF-8 兼容处理 |
| | `webapp.py` + `web/index.html` | Web 仪表盘与 API（对话、页面批准确认、记忆/声纹/音色管理），端口 8080 |
| | `voice/__main__.py` | 语音模式入口（麦克风输入 / 键盘输入 / 唤醒词待机） |
| | `check_config.py` | 配置与环境自检（LLM 连通、设备可读、存储可写） |
| **Agent 层** | `agent.py` | Agent 循环：LLM↔工具往返、调用上限、异常收敛 |
| | `planner.py` | 多步骤任务规划（计划验证→顺序执行→失败中止） |
| | `conversation.py` / `token_budget.py` | 对话上下文裁剪 / 每日 token 预算 |
| | `llm_client.py` | LLM 适配层（OpenAI 兼容协议，多模型档案 `LLM_<角色>_*`） |
| **设备层** `devices/` | `base.py` | 设备抽象接口（`Device`/`ControllableDevice` + 异常语义） |
| | `registry.py` | 设备注册表（线程安全、原子换血） |
| | `simulated.py` | 模拟设备（温湿度/灯/风扇，状态持久化 JSON） |
| | `homeassistant.py` | Home Assistant REST 适配器（自动发现+动态能力） |
| | `ha_ws.py` / `discovery.py` | HA WebSocket 订阅（即时感知）/ 设备自动发现刷新 |
| | **`gpio.py`** ⭐ | **树莓派 40-pin IO 直连**：LED/PWM 灯、三档风扇、继电器、DHT11/22、DS18B20、PIR |
| **语音层** `voice/` | `tts.py` | TTS 桥接（SAPI/espeak）+ faster-whisper 识别 + 麦克风采集 + 跨平台播放 |
| | `wakeword.py` | sherpa-onnx 离线唤醒词（默认「小管家」） |
| | `cosyvoice_client.py` | CosyVoice 服务端 HTTP 客户端（合成/声纹向量） |
| | `profiles.py` / `speakers.py` | 音色档案库（克隆音色）/ 成员声纹注册与识别 |
| | `assembly.py` | 语音子系统装配（按 `TTS_BACKEND` 组装主/备降级链） |
| **记忆与安全** | `memory/store.py` | SQLite 长期记忆、任务日志、通知、审计、token 用量 |
| | `confirmation.py` / `tools/executor.py` | 确认制（控制台/Web 队列）+ 安全执行器（Schema 校验→风险→确认→回读→审计） |
| | `tools/home.py`、`memory_tools.py`、`registry.py`、`validation.py` | 设备查询/控制工具、记忆工具、工具注册表、参数校验 |
| **主动服务** | `proactive.py` / `scheduler.py` / `routines.py` | 主动提醒规则引擎 / 后台调度 / 定时例行任务（预授权） |
| **家庭生活** `household/` | `pantry.py` / `wardrobe.py` | 食材管家（入库/临期提醒）/ 衣柜管家（穿着记账/穿搭推荐） |
| | `store.py` / `checks.py` / `weather.py` | 台账 SQLite 库 / 每日巡检 / Open-Meteo 天气 |
| **联调工具** | `emulator/ha.py` | HA REST API 本地模拟器（无 HA 也能联调适配器） |

## 2️⃣ 配置 `config/`

| 文件 | 功能 |
|---|---|
| `ha_devices.example.json` | HA 设备白名单示例（8 类设备，高风险域排除） |
| `gpio_devices.example.json` ⭐ | GPIO 设备清单示例（BCM 引脚、PWM、传感器 kind） |
| （gitignore 的实际配置） | `ha_devices.json` / `gpio_devices.json` 使用时从 example 复制 |

## 3️⃣ 固件 `firmware/esphome/`

| 文件 | 功能 |
|---|---|
| `esp32-butler.yaml` | ESP32 固件配置（DHT22 + 测试灯，ESPHome 自动发现到 HA） |
| `secrets.yaml(.example)` | WiFi 与 API 加密密钥（不提交） |

## 4️⃣ 脚本 `scripts/`

| 分类 | 文件 | 功能 |
|---|---|---|
| **Pi 部署** `pi/` | `01_base.sh`→`05_gpio.sh` ⭐、`check.sh` | 树莓派一键部署（系统/HA 容器/管家服务/语音/**GPIO 依赖自检**）+ 部署自检 |
| **备份** | `backup.sh` ⭐ / `backup.ps1` | Pi 侧 / Windows 侧数据备份（保留最近 10 份） |
| **CosyVoice** | `cosyvoice_*.py/bat` | PC 端语音合成服务（3GB 模型，Pi 跨机调用） |
| **其他** | `wakeword_download_model.py/.bat`、`setup_homeassistant.ps1` | 唤醒词模型下载 / PC 端 HA 容器部署 |

## 5️⃣ 文档 `docs/` 与测试 `tests/`

| 分类 | 内容 |
|---|---|
| 需求/设计 | `REQUIREMENTS.md`（验收）、`ARCHITECTURE.md`、`TECH_STACK_DECISIONS.md`、`ROADMAP.md` |
| 运维 | `RUNBOOK.md`（运行维护）、`OPEN_SOURCE_REUSE.md`（复用记录） |
| **硬件（重点）** | `HARDWARE.md`（ESP32 接入）、`PI_DEPLOYMENT.md`（Pi 部署手册）、**`PI_GPIO_TESTING.md`** ⭐（GPIO 接线表+安全红线+可执行性分类报告+验证清单） |
| 语音 | `VOICE.md`、`COSYVOICE_SETUP.md` |
| 测试 `tests/` | 31 个文件 / 351 项：设备/执行器/规划器/语音/唤醒词/家庭台账/**GPIO 后端**⭐、真实 API 集成测试（默认跳过） |

## 6️⃣ 运行时与本地目录（不进 git）

| 目录 | 功能 |
|---|---|
| `data/` | `memory.db`（长期记忆）、`household.db`（家庭台账）、设备状态、唤醒词模型 |
| `logs/` | 滚动文件日志 |
| `_local/` | 本地开发运行器（`run.cmd`/`local_run.py`/`hw_probe.py`，Windows 专用，不提交） |
| `.venv/` | Python 虚拟环境（Pi 上会重建为 `.venv/bin/`） |

## 附：树莓派可执行性速查

- ✅ 直接可跑：全部核心模块、simulated/HA 后端、whisper 识别、唤醒词、HA 模拟器
- ✅ 本次改造后可跑：GPIO 直连后端、CosyVoice 音频播放（aplay 等）、espeak 兜底 TTS
- ❌ 保留在 Windows/PC：`*.bat`、`*.ps1`、`_local/` 运行器、CosyVoice 服务端（3GB 模型）、
  ESPHome 固件编译（能跑但慢，建议 PC 编译）

⭐ = pi_ceshi 分支本次新增/重点改动。
