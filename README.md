# AI Home Butler（AI 智能家居管家）

在成熟开源智能家居基础设施之上构建的 AI 家庭管家：具备环境理解、记忆、任务规划、
安全执行与主动服务能力。**不重复造 Home Assistant，不重写语音引擎。**

当前状态：**阶段 1 完成** —— 最小可运行 AI 管家（CLI + 模拟设备 + 工具调用 + 安全确认）。

- 需求与验收：[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)
- 架构设计：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 技术选型：[docs/TECH_STACK_DECISIONS.md](docs/TECH_STACK_DECISIONS.md)
- 开源复用记录：[docs/OPEN_SOURCE_REUSE.md](docs/OPEN_SOURCE_REUSE.md)
- 路线图：[docs/ROADMAP.md](docs/ROADMAP.md)

## 阶段 1 能做什么

- 命令行多轮自然语言对话（简体中文）。
- 读取模拟温湿度传感器（数据带采样时间与新鲜度标记）。
- 查询/控制模拟设备：灯（开关、亮度 1-100）、风扇（开关、档位 low/medium/high）。
- **控制操作必须人工确认**；拒绝则不执行、状态不变（程序保证，不靠提示词）。
- 操作后回读设备状态验证；状态未知或失败绝不报告成功。
- 模拟设备状态持久化（`data/simulated_state.json`），重启不丢。
- 模型服务异常自动重试；工具调用次数上限保护；全程结构化日志与 Token 用量记录。

模型接入为 OpenAI 兼容适配层，默认 DeepSeek，可经 `.env` 换任何兼容厂商。

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
python -m pytest tests/            # 离线回归（41 项，不触网、零 API 成本）
BUTLER_RUN_REAL_API_TESTS=1 python -m pytest tests/test_integration_real_api.py
                                   # 真实 API 集成测试（默认跳过）
```

## 项目结构

```
butler/
├── cli.py            # 命令行交互（UTF-8 兼容 Windows GBK 终端）
├── agent.py          # Agent 循环：LLM↔工具往返、上限保护、异常收敛
├── llm_client.py     # LLM 适配层（OpenAI 兼容，可换厂商）
├── conversation.py   # 对话上下文（按整轮裁剪）
├── confirmation.py   # 确认接口（控制台 / 测试注入）
├── config.py         # 配置加载与校验（fail-fast）
├── logging_setup.py  # 滚动文件日志
├── devices/          # 设备抽象 + 模拟设备（传感器/灯/风扇）+ 注册表
└── tools/            # 工具注册表、参数校验、安全执行器、家庭工具集
tests/                # pytest：校验/设备/执行器/Agent/配置（离线）+ 真实 API 集成
docs/                 # 需求/架构/选型/复用记录/路线图
```

安全层执行顺序：注册检查 → JSON 解析 → Schema 校验 → 风险等级 → 用户确认 → 执行 →
回读验证 → 审计日志。高风险操作（门锁/燃气/电热类）默认禁止，无注册工具。

## 已知限制（阶段 1）

- 设备为模拟实现，无真实硬件（阶段 2 接入 Home Assistant，阶段 3 接入 ESP32）。
- 无长期记忆（阶段 5）；无多步骤任务规划（阶段 6）；无语音（阶段 8）。
- `CONFIRMATION_MODE=never` 仅供调试，会跳过所有确认并输出警告日志。
- 模拟传感器读数为随机漂移，不代表真实环境。

## 安全提示

- API Key 只保存在本地 `.env`（已在 `.gitignore`），不要提交或分享。
- 若 Key 曾在聊天等场合明文传递，建议到服务商控制台轮换。
