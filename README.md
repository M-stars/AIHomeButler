# AI Home Butler（AI 智能家居管家）

在成熟开源智能家居基础设施之上构建的 AI 家庭管家：具备环境理解、记忆、任务规划、
安全执行与主动服务能力。**不重复造 Home Assistant，不重写语音引擎。**

当前状态：**阶段 0-11 首轮全部完成** —— 文本/网页/语音三种交互，模拟与 Home Assistant
双设备后端，长期记忆，多步骤任务规划，主动提醒，语音播报；真实 HA 容器与 ESP32 烧录
各差一步用户操作（见 docs/ROADMAP.md「近期待办」）。

- 需求与验收：[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)
- 架构设计：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 技术选型：[docs/TECH_STACK_DECISIONS.md](docs/TECH_STACK_DECISIONS.md)
- 开源复用记录：[docs/OPEN_SOURCE_REUSE.md](docs/OPEN_SOURCE_REUSE.md)
- 运行维护手册：[docs/RUNBOOK.md](docs/RUNBOOK.md)
- 硬件接入：[docs/HARDWARE.md](docs/HARDWARE.md) ｜ 语音：[docs/VOICE.md](docs/VOICE.md)
- 路线图：[docs/ROADMAP.md](docs/ROADMAP.md)

## 能做什么

- **文本 CLI**（`python -m butler`）：多轮对话、传感器查询、设备控制（确认制）。
- **Web 界面**（`python -m butler.webapp`）：仪表盘+对话+页面批准确认+记忆管理+任务记录。
- **语音模式**（`python -m butler.voice`，`--mic` 语音输入）：SAPI 离线播报 + 本地 whisper 识别。
- **长期记忆**：用户确认后保存偏好/事实，随时查看/修改/删除/导出，立即影响决策。
- **多步骤任务**：`/task 我准备睡觉了，关掉卧室灯并打开风扇低档` —— 计划先验证再执行，
  逐步可追踪，出错即中止。
- **主动提醒**：对话里说「温度超过 30 度提醒我」即可创建规则（连续命中+冷却防霸屏，
  纯通知，绝不自动控制设备）。
- **双设备后端**：`DEVICE_BACKEND=simulated`（默认，安全）或 `homeassistant`（真实设备，
  白名单管控），Agent 层零改动切换。

## 快速开始

环境要求：Windows 10/11，Python 3.9+（实测 3.9.13）。

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置密钥（复制示例文件，填入你的 API Key；.env 不会被提交）
copy .env.example .env
#    编辑 .env 填入 LLM_API_KEY

# 3. 运行
python -m butler
```

对话示例：

```
你: 现在客厅温度是多少？
管家: 客厅当前温度 22.2°C，湿度 50.1%（数据为最新）。

你: 把客厅灯打开
[需要确认] 控制设备 light.living_room：turn_on
  参数: {"device_id": "light.living_room", "action": "turn_on"}
是否执行？[y/N]: y
管家: 客厅灯已打开，当前状态：开启，亮度 80。
```

命令：`/devices` 列出设备、`/help` 帮助、`/exit` 退出。

## 运行测试

```bash
python -m pytest tests/            # 离线回归（91 项，不触网、零 API 成本）
BUTLER_RUN_REAL_API_TESTS=1 python -m pytest tests/test_integration_real_api.py
                                   # 真实 API 集成测试（默认跳过）
python -m butler.check_config      # 配置与环境自检
```

## 项目结构

```
butler/
├── cli.py            # 命令行交互（UTF-8 兼容 Windows GBK 终端）
├── webapp.py         # Web 仪表盘与 API（确认队列/并发保护）
├── agent.py          # Agent 循环：LLM↔工具往返、上限保护、异常收敛
├── planner.py        # 多步骤任务规划器（计划验证→顺序执行→中止语义）
├── proactive.py      # 主动服务规则引擎（纯通知、确定性规则）
├── llm_client.py     # LLM 适配层（OpenAI 兼容，可换厂商）
├── conversation.py   # 对话上下文（按整轮裁剪）
├── confirmation.py   # 确认接口（控制台 / Web 队列 / 测试注入）
├── check_config.py   # 配置与环境自检
├── config.py         # 配置加载与校验（fail-fast）
├── logging_setup.py  # 滚动文件日志
├── memory/           # SQLite 长期记忆、任务日志、规则与通知
├── devices/          # 设备抽象 + 模拟设备 + Home Assistant 适配器 + 注册表
├── emulator/         # HA REST API 本地模拟器（无 HA 也能联调适配器）
├── voice/            # 语音桥接：SAPI TTS + faster-whisper ASR + 麦克风
└── tools/            # 工具注册表、参数校验、安全执行器、家庭/记忆/规则工具集
firmware/esphome/     # ESP32 固件配置（已通过校验并完成编译）
scripts/              # HA 一键部署、备份脚本
tests/                # 92 项测试：离线回归 + 真实 API 集成（默认跳过）
docs/                 # 需求/架构/选型/复用/路线图/RUNBOOK/硬件/语音
```

安全层执行顺序：注册检查 → JSON 解析 → Schema 校验 → 风险等级 → 用户确认 → 执行 →
回读验证 → 审计日志。高风险操作（门锁/燃气/电热类）默认禁止，无注册工具。

## 已知限制

- 真实 Home Assistant 容器待部署：Docker Desktop 已装好，**需重启一次电脑完成 WSL2**，
  然后管理员运行 `scripts\setup_homeassistant.ps1`（详见 docs/RUNBOOK.md 第 3 节）。
- ESP32 待烧录：固件已编译完成，插线后 `esphome run firmware/esphome/esp32-butler.yaml`。
- 语音输入的噪声环境表现未做真实调试；无唤醒词（演进方向 HA Voice，见 docs/VOICE.md）。
- `CONFIRMATION_MODE=never` 仅供调试，会跳过所有确认并输出警告日志。
- Web 界面无登录鉴权，默认仅绑定 127.0.0.1，勿暴露公网。
- 主动服务为纯通知级：设备自动化交给 HA 内置自动化承担（安全设计）。

## 安全提示

- API Key 只保存在本地 `.env`（已在 `.gitignore`），不要提交或分享。
- 若 Key 曾在聊天等场合明文传递，建议到服务商控制台轮换。
