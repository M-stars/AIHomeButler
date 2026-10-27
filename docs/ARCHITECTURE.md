# AI Home Butler 架构文档（ARCHITECTURE）

更新日期：2026-10-28 ｜ 状态：阶段 1 架构（含阶段 2/3 演进方向；阶段 12 语音子系统见 §6）

## 1. 逻辑分层（长期目标）

```
┌─────────────────────────────────────────────────┐
│ 交互层   CLI(阶段1) → Web界面(阶段9) / 语音(阶段8) │
├─────────────────────────────────────────────────┤
│ AI Agent 核心（NLU / 对话上下文 / 工具调用 / 规划）  │
│   记忆系统(阶段5)  主动服务(阶段7)                 │
├─────────────────────────────────────────────────┤
│ 安全与权限层（工具白名单/参数校验/确认/验证/审计日志）│  ← 独立于提示词，程序强制
├─────────────────────────────────────────────────┤
│ 统一工具系统（注册表 / Schema / 风险等级 / 执行器）  │
├─────────────────────────────────────────────────┤
│ 设备适配层   模拟设备(阶段1) → HA(阶段2) → ESPHome(阶段3) │
│ 家庭状态层（实时/历史/推断/未知 严格区分）           │
└─────────────────────────────────────────────────┘
```

原则：**LLM 只与工具系统对话，永远不直接接触设备或网络端口**；安全检查在程序层，
不依赖系统提示词。

## 2. 阶段 1 模块结构（当前实现）

```
butler/
├── __main__.py          # python -m butler 入口
├── cli.py               # 交互层：输入输出、UTF-8 处理、命令
├── config.py            # 配置加载与校验（.env，fail-fast）
├── logging_setup.py     # 滚动文件日志 + 控制台
├── llm_client.py        # LLMClient 协议 + OpenAICompatClient（DeepSeek 等）
│                        #   + 重试/超时；LLMError 统一异常
├── conversation.py      # 对话上下文：消息管理、按"整轮"裁剪防膨胀
├── agent.py             # Agent 循环：LLM↔工具往返、工具上限保护、异常收敛
├── confirmation.py      # Confirmer 协议：ConsoleConfirmer / AutoApprove / AutoDeny
├── devices/
│   ├── base.py          # Device 基类、DeviceError/DeviceOffline/DeviceNotFound
│   ├── registry.py      # 设备注册表（统一按 id 查询）
│   └── simulated.py     # 模拟温湿度传感器/灯/风扇 + JSON 状态持久化
└── tools/
    ├── base.py          # ToolSpec(name/schema/risk/handler)、ToolResult 状态机
    ├── registry.py      # 工具注册表（未注册工具不可执行）
    ├── validation.py    # JSON Schema 子集校验（类型/必填/枚举/范围）
    ├── executor.py      # 执行流水线：查找→解析→校验→风险→确认→执行→回读验证→审计
    └── home.py          # 阶段1具体工具：list_devices/get_device_state/
                         #   get_sensor_readings/control_device
tests/                  # pytest，全部离线（FakeLLM / AutoConfirmer / tmp_path）
```

## 3. 关键数据流

一次用户输入的处理：

```
用户文本
  → Conversation 记录
  → Agent 循环（最多 MAX_TOOL_CALLS_PER_TURN 次）：
      LLM.chat(messages, tools) 
        ├─ 返回 tool_calls → 逐个交给 ToolExecutor：
        │     1 未注册工具 → error 结果回传模型
        │     2 JSON 解析失败 → error（模型可自纠）
        │     3 Schema 校验失败 → error（附具体原因）
        │     4 风险等级 HIGH → 直接拒绝（阶段1不开放）
        │     5 MEDIUM 且需确认 → Confirmer；拒绝 → declined，不执行
        │     6 执行 handler（设备层做存在性/在线预检查）
        │     7 控制类操作 → 回读状态验证；读失败 → 状态 unknown
        │     8 全程写审计日志
        │   工具结果以 role=tool 消息回传模型，进入下一轮 LLM 调用
        └─ 返回文本 → 作为本轮回复结束
  → 上限触达：注入"禁止再调工具"系统消息，强制无工具收敛一次
```

ToolResult.status 明确区分：`ok / error / declined / unknown`。
控制类结果额外携带 `verified: true|false` 与 `state_after`。**"unknown"不得被表述为成功。**

## 4. 关键接口（为阶段 2/3 预留的替换点）

| 接口 | 阶段1实现 | 阶段2 | 阶段3 |
|---|---|---|---|
| `LLMClient.chat()` | OpenAI 兼容（DeepSeek） | 不变 | 不变 |
| `DeviceRegistry` / `Device` | Simulated* | HomeAssistantAdapter（REST API） | 增补 ESPHome 设备（经 HA 统一） |
| `Confirmer` | 控制台 | 不变 | 语音/界面确认复用同一协议 |
| 工具集 home.py | 4 个模拟工具 | 同名工具改走 HA adapter，Agent 层无感 | — |

设备适配器必须提供：统一的能力描述（类型/区域/可用性）、读写接口、
`DeviceOffline`/`DeviceNotFound` 异常语义、状态读取带 `sampled_at` 时间戳。

## 5. 决策与取舍记录

- **单 Agent + 工具注册表**，不引入多 Agent/编排框架：阶段 1 需求为单轮内"LLM↔工具"往返，
  官方 SDK 的 function calling 已覆盖，多 Agent 是提前抽象。
- **不引入向量数据库**：阶段 1 只有对话上下文；阶段 5 记忆先用 SQLite/结构化文件评估。
- **不做自定义前端**：CLI 足够验证核心闭环；界面是阶段 9 的事。
- **设备状态持久化用 JSON 文件**：仅 3 个模拟设备，无并发写入；SQLite 留给阶段 5 记忆。
- **确认策略**：读=LOW 自动放行；控制=MEDIUM 必确认；HIGH（锁/燃气/电热）默认无注册工具、
  执行器层面直接拒绝——这是代码保证，而非提示词约定。
- **Windows GBK 终端**：CLI 启动时对 stdout/stderr 做 UTF-8 reconfigure（带降级保护）。

## 6. 语音子系统（阶段 12 增补）

```
交互层（voice CLI / Web 界面）
      │ 只把"最终回复文本"交给语音层
      ▼
Speaker 抽象（butler/voice/tts.py）
  ├─ SapiSpeaker        Windows SAPI5 离线合成（默认，历史行为不变）
  ├─ CosyVoiceSpeaker   官方 CosyVoice 服务端合成（可选后端）
  │     │ HTTP 127.0.0.1:9880（httpx，超时/文本长度/并发上限在客户端收敛）
  │     ▼
  │   官方推理代码 + 薄适配层（scripts/cosyvoice_server.py：官方 server 与
│   CosyVoice3 前端不兼容，实测后按最薄原则重写路由，复用官方 AutoModel）
  │     └─ 官方推理代码 + Fun-CosyVoice3-0.5B 模型（独立 Python 3.10 环境）
  ├─ FallbackSpeaker    主后端故障按配置降级；降级留痕（last_degradation）
  └─ AudioCache         生成音频 LRU 缓存（数据目录内，可按音色清除）
VoiceProfileManager（butler/voice/profiles.py）
  └─ 音色档案 CRUD + 参考录音管理（数据目录）+ 授权门禁
     （consent_status != granted 的档案禁止克隆合成，合成层强制检查）
```

职责边界：ASR/Agent/TTS/音色管理/播放各自独立；TTS 不接触设备接口与
Home Assistant，不绕过确认流程；语音层任何异常不得中断 Agent 主流程
（voice CLI 的播报全部经 _speak_safely 收敛异常）。

安全约束：参考录音与生成音频只存数据目录（.gitignore 覆盖）；Web 端点
不暴露录音绝对路径；合成语音在界面上明确标注"AI 合成"。
