# 开源复用记录（OPEN_SOURCE_REUSE）

本项目最高优先级规则：**先找现成实现，再决定是否开发。**
每个重要功能动手前，在此登记检索记录。检索日期均为 2026-10-27。

## 1. 已采用的现成实现（阶段 0/1）

| # | 功能 | 采用方案 | 仓库/文档 | 许可证（已核实） | 维护状态 | 用法 | 是否二次开发 |
|---|---|---|---|---|---|---|---|
| 1 | LLM API 访问 + 工具调用 | OpenAI Python SDK（DeepSeek 官方推荐接入方式：仅改 base_url） | github.com/openai/openai-python | Apache-2.0 | 活跃（2026-10-26 仍有推送） | pip 依赖，直接使用 | 否 |
| 2 | 大模型服务 | DeepSeek API（`deepseek-chat`） | api-docs.deepseek.com | 商业 API，按其服务条款 | 官方服务 | REST 调用 | 否 |
| 3 | 环境变量/密钥管理 | python-dotenv | github.com/theskumar/python-dotenv | BSD-3-Clause | 活跃 | pip 依赖 | 否 |
| 4 | 自动化测试 | pytest | github.com/pytest-dev/pytest | MIT | 活跃 | pip 依赖 | 否 |
| 5 | 智能家居平台（阶段 2 采用） | Home Assistant Core | github.com/home-assistant/core | Apache-2.0 | 活跃（2026-10-26 推送） | 部署为独立服务 | 否 |
| 6 | HTTP 客户端（HA API 调用） | httpx | github.com/encode/httpx | BSD-3-Clause | 活跃 | pip 依赖 | 否 |
| 7 | Web 界面与 API（阶段 9） | FastAPI + Uvicorn + Pydantic | github.com/fastapi/fastapi | MIT | 活跃 | pip 依赖 | 否 |
| 8 | 语音合成（阶段 8 桥接） | pyttsx3 → Windows SAPI5 | github.com/nateshmbhat/pyttsx3 | MPL-2.0 | 可用 | pip 依赖 | 否 |
| 9 | 语音识别（阶段 8 桥接，可选） | faster-whisper（CTranslate2 移植） | github.com/SYSTRAN/faster-whisper | MIT | 活跃 | pip 依赖（可选） | 否 |
| 10 | 麦克风采集（可选） | sounddevice | python-sounddevice.readthedocs.io | MIT | 活跃 | pip 依赖（可选） | 否 |
| 11 | ESP32 固件（阶段 3 采用） | ESPHome 2025.5.2（本机已安装并完成固件编译） | github.com/esphome/esphome | GPL-3.0 + 附加授权（NOASSERTION，已人工确认 LICENSE：作为工具使用、固件产物归配置作者，合规） | 活跃 | pip 安装的工具链 | 仅写 YAML 配置 |

检索关键词（#1）：`DeepSeek API OpenAI SDK compatible base_url function calling`；
核实方式：官方文档 api-docs.deepseek.com + 本机实测（tool_calls 与结果回传全链路通过）。

## 2. 已检索、暂缓/不采用的方案

| # | 候选 | 检索结论 | 不采用/暂缓原因 |
|---|---|---|---|
| 1 | wukong-robot（悟空机器人，wzpan/wukong-robot） | MIT 许可证；GitHub API 显示最后推送 **2024-10-25**，停滞约两年 | 维护停滞；与 HA Voice 生态相比已落后；其能力（ASR/TTS/唤醒）后期由 HA 生态覆盖更稳 |
| 2 | Home Assistant Voice / Wyoming 协议 | 官方活跃（voice-pe 硬件 + wyoming 集成，home-assistant.io/integrations/wyoming） | **阶段 8 语音的首选**；当前无语音需求，暂缓引入 |
| 3 | ESPHome（esphome/esphome） | 活跃（changelog 2026.10.0）；GitHub license 字段 NOASSERTION | ~~阶段 3 固件首选~~ → **已采用**（见上表 #11）；已知问题：DHT22 读失败返回假值（esphome/issues#397），管家侧以 sampled_at 新鲜度兜底 |
| 4 | ESP-IDF / Arduino-ESP32 / PlatformIO | 官方 SDK 成熟（ESP-IDF 为 Apache-2.0，阶段 3 引入时再核实其余） | 仅当 ESPHome 无法满足定制需求时启用（阶段 3 决策点） |
| 5 | Home Assistant 部署方式（Windows） | 官方建议 Windows 上以 HAOS 虚拟机运行；Docker Desktop 容器为轻量替代；WSL2 Supervised 为社区方案 | Docker 未安装 → 阶段 2 需先安装 Docker Desktop 或 VirtualBox；这是用户需完成的操作 |
| 6 | LangChain / AutoGen / CrewAI 等 Agent 框架 | 能力覆盖工具编排，但抽象重、依赖面大 | 阶段 1 的"单 Agent + function calling + 自有执行器"即可满足且可控性/可测试性更好；待阶段 6 任务规划时重新评估 |

## 3. 明确禁止的重复开发（对照检索结论）

- 设备管理/发现/自动化引擎 → Home Assistant 已内置（阶段 2 直接复用）。
- MQTT 客户端/协议 → 优先 `paho-mqtt` 或 HA 内置能力（需要时引入）。
- 语音识别/合成/唤醒 → HA Voice + Wyoming 生态（阶段 8）。
- 大模型推理/训练 → DeepSeek API。
- Agent 编排 → 先用官方 SDK function calling（见上表 #6）。

## 4. 供应链与合规要求（每次引入依赖时执行）

1. 核实 LICENSE（本文件登记 spdx_id；GitHub API 或仓库 LICENSE 文件）。
2. 记录直接依赖；评估重要间接依赖数量与维护情况。
3. 遵守版权与再分发条款；不复制未授权代码。
4. 不提交密钥/个人信息（.env 已在 .gitignore）。
5. 引入前用 `pip show`/`pip-audit` 做版本与漏洞基本检查。
6. 许可证未确认的代码：只参考公开接口与设计，寻找可合法替代方案。

## 5. 阶段 2/3 待检索清单（预告）

- HA REST API / WebSocket API 的实体查询与服务调用边界（developer.api.home-assistant.io）。
- `homeassistant-api` 等现成 Python 客户端是否优于直接 httpx 调用。
- ESPHome 在无 Docker 环境下的 Windows 安装（`pip install esphome`）与串口烧录流程。
- ESP32 具体型号确认后选择 DHT22/SHT 系列温湿度组件。
