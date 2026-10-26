# 技术选型决策（TECH_STACK_DECISIONS）

更新日期：2026-10-27 ｜ 每条决策含理由与被否方案，环境依据见 REQUIREMENTS.md 第 2 节。

## 1. 已定决策（阶段 0/1）

| 领域 | 决策 | 理由 | 被否方案及原因 |
|---|---|---|---|
| 主语言 | Python 3.9（本机已有） | AI 生态最全；本机实测 3.9.13 可用 | Node.js：LLM/Agent 生态更弱；需用户另装运行时 |
| LLM 接入 | **openai 官方 SDK + `base_url` 指向 DeepSeek** | DeepSeek 官方文档明确支持；实测 function calling 全链路通过；换厂商=改 .env 两个值 | deepseek 官方独立 SDK：本质也是 OpenAI 兼容封装，多一层无谓依赖；裸 httpx：重复造鉴权/重试/超时轮子 |
| 模型 | `deepseek-chat`（服务端实际解析为 deepseek-flash） | 用户指定；工具调用可靠 | 其他厂商：无密钥；已通过适配层保证可替换 |
| 对话界面 | CLI（stdin/stdout） | 阶段 1 目标是验证核心闭环，界面是阶段 9 | Web 前端：过早投入，AI 核心未稳 |
| 工具调用协议 | OpenAI function calling 格式 | 官方规范、模型训练充分、DeepSeek 兼容 | 自定义 XML/文本协议：可靠性差、需自行解析 |
| 安全层 | 独立 ToolExecutor：校验→风险→确认→执行→回读验证 | 提示词不可作为安全边界（需求书 4.5 节） | 仅靠 system prompt 约束：可被提示注入绕过 |
| 模拟设备状态存储 | JSON 文件（data/，gitignore） | 3 个模拟设备、单进程、可读易调试 | SQLite：现阶段无并发/查询需求，阶段 5 记忆再引入 |
| 日志 | logging + RotatingFileHandler（logs/，UTF-8） | 标准库即可满足滚动需求 | loguru/structlog：多依赖，阶段 8 可观测性再评估 |
| 测试 | pytest + FakeLLM/AutoConfirmer 注入 | 全离线、确定性、零 API 成本 | 真实 API 回归测试：慢、贵、不确定；仅做一次性冒烟 |
| 配置 | .env + fail-fast 校验 + .env.example | 密钥不入库；缺失配置直接报错并给出修复指引 | 配置缺失时给默认值继续跑：违反"不得因配置缺失放开权限" |

## 2. 环境风险与对策

| 风险 | 对策 |
|---|---|
| Python 3.9 偏旧，部分新包不再支持 | 依赖仅选兼容 3.9 的版本（pip 自动解析）；代码不用 3.10+ 语法（`X|Y`、match 等）；**建议**用户后续装 3.11+，非强制 |
| Windows 终端 GBK 编码 | CLI 启动时 stdout/stderr UTF-8 reconfigure；文件 IO 显式 encoding="utf-8" |
| Docker 未安装 | 不阻塞阶段 1；阶段 2 前需安装 Docker Desktop（或 VirtualBox 跑 HAOS），已列入用户待办 |
| ESP32 工具链未安装 | 阶段 3 决策点：确认型号后装 PlatformIO 或 `pip install esphome` |

## 3. 阶段 2 预决策（暂不实施）

- HA 部署：优先 Docker Desktop 容器（Core 版，无插件需求即可满足 AI 接入）；
  若需完全体（插件/加载项）则 HAOS 虚拟机。
- HA 接入：优先官方 REST API（长连接需求出现后再评估 WebSocket），
  通过 `HomeAssistantAdapter` 实现既有 `Device` 接口，Agent 与工具层零改动。
- 阶段 2 开发期保留模拟模式：`DEVICE_BACKEND=simulated|homeassistant` 双轨，
  测试永远跑 simulated，防止误控真实家电。

## 4. 变更流程

任何选型变更：先在本文档追加"变更记录"（日期/动机/对比/影响面），再改代码。
