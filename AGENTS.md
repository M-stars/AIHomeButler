# AGENTS.md —— AI 协作上手指南

> 本文件写给在本仓库工作的 AI 编码助手（也适合新人工程师快速入门）。
> 生成：2026-11-21 ｜ 基准 commit：`1a39a23`（main）。**改动核心机制后请同步更新本文件。**

---

## 1. 项目一句话定位

**AI Home Butler**：在成熟开源智能家居基础设施（Home Assistant）之上构建的 AI 家庭管家。
Python 单体应用，核心命题是——**用确定性的软件工程机制（白名单/Schema 校验/确认制/回读验证/审计）
去约束不确定的 LLM 输出**，让大模型安全地控制真实家居设备。

- 性质：毕业设计（软件工程），课题「基于树莓派与大语言模型的边缘多模态智能家居控制系统」
  （见 `docs/GRADUATION_PROPOSAL.md`）。
- 工程哲学（贯穿所有代码，勿违背）：
  - **不重复造 Home Assistant，不重写语音引擎**——只做适配与增强；
  - **LLM 只与工具系统对话，永远不直接接触设备或网络端口**（`docs/ARCHITECTURE.md` §1）；
  - **安全检查在程序层，不依赖系统提示词**——提示词只是体验层，代码才是强制层；
  - **不引入多 Agent/编排框架、不引入向量数据库**——单 Agent + 工具注册表已覆盖需求；
  - 可选功能一律「依赖未装自动降级」，主依赖极轻（运行时仅 8 个包，见 `requirements.txt`）。

## 2. 三条红线（任何改动不得触碰）

1. **确认制不可绕过**：控制类操作（risk=medium）必须经 `Confirmer` 人工确认；
   高风险（risk=high）在执行器层面直接拒绝，且系统内**故意没有注册**任何高风险工具。
   门锁/报警器/警笛/热水器（HA 域 `lock/alarm_control_panel/siren/water_heater`）与
   车库/院门（cover 的 `garage/gate` device_class）在 `butler/devices/discovery.py:66-71`
   被**结构性排除**——这是代码保证，不是约定。新增功能不得放松。
2. **密钥只存本地 `.env`**（已被 `.gitignore`），任何情况下不得提交/打印/写进日志
   （日志只显示 Key 前 6 位）。`.env`、`config/ha_devices.json`、`config/gpio_devices.json`、
   `firmware/esphome/secrets.yaml` 都不进仓库。推 GitHub 前先扫描密钥。
3. **失败语义诚实**：`ToolResult.status` 只有 `ok / error / declined / unknown` 四态，
   回读失败= `unknown`，**绝不允许把 unknown 表述为成功**（`butler/devices/base.py:34-35`）。
   控制类结果必须带 `verified` 与 `state_after`。

## 3. 当前状态快照（2026-11-21）

- **阶段 0-14.7 首轮全部完成**：三交互入口（CLI/Web/语音）、模拟+HA 双设备后端（真机 47 台）、
  长期记忆、多步任务规划、主动提醒、定时提醒、预授权例行任务、声纹识别、唤醒词待机、
  CosyVoice 音色库、食材管家+衣柜管家（阶段 14.7）。
- **V2.0 已合并主干**（commit `635bff5` + `1a39a23`）：树莓派 GPIO 直连后端、HA 熔断器、
  App/小程序多令牌配对、设备直控 API、SSE 安全流水线、Web 前端重构（PWA）、GitHub Actions CI。
- **移动端 PE 端 App 已落地**（2026-11-21，`aihome-butler-mobile/`，uni-app Vue3+TS+Pinia+Vite）：
  设计与验收以 `docs/MOBILE_APP_SPEC.md` 为准，UI 视觉对齐 `butler/web/` Web 控制台；
  H5/微信小程序产物构建通过，73 项 Vitest 独立离线测试全绿（`cd aihome-butler-mobile && npm run test`）；
  配对/直控/确认审批/流水线/踢除 401 已对模拟后端端到端实测；**签名 APK 已产出**
  （`android-app/`，Capacitor 7 壳工程本地 Gradle 构建，keystore 不入 git，
  打包步骤与踩坑见 `android-app/README.md`；真机安装联调待做）。
- PC 桌面壳曾在本地 `desktop-shell` 分支出现过，**主干已移除**——应用路线以
  `docs/APP_DESIGN.md`（V2：配对制 + 直控 + 云中转，阶段 15-18）为准。
- 测试：33 个文件约 364 个测试函数，全部离线；README 里的"327 项"、REPO_STRUCTURE 里的"351 项"
  是旧数字，**以 `pytest -q` 实际输出为准**。
- ⚠️ `docs/ROADMAP.md` 更新于 2026-11-01，其中"待办 4：HA 熔断器"已在 V2.0 完成，
  ROADMAP 尚未回填——**判断现状以代码 + 本文件为准，文档滞后的要顺手回填**。
- ⚠️ `scripts/pi/` 部署脚本与 `docs/PI_DEPLOYMENT.md` **均未在真机验证过**（Pi 未采购/到货）。
- 下一步主线：Pi 真机部署跑通 → ESP32 插线烧录 → 阶段 14.7 真机体验 → 应用阶段 15-18
  （管家侧 API 已就绪，缺 PC/移动端客户端）。

## 4. 环境与常用命令

- **Python 3.9 主环境**（实测 3.9.13；CI 用 3.11）。写代码须 3.9 兼容语法
  （不能用 `match`、`X | Y` 类型标注）。
- Windows 10/11 开发；树莓派为部署目标（ARM64）。

```bash
pip install -r requirements.txt        # 安装依赖
python -m butler                       # 入口 1：文本 CLI（/devices /memory /task /help /exit）
python -m butler.webapp                # 入口 2：Web 界面，默认 127.0.0.1:8080，启动打印带 ?token= 的地址
python -m butler.voice [--mic] [--wake]# 入口 3：语音模式（--mic 麦克风 ASR，--wake 唤醒词待机）
python -m butler.check_config          # 配置与环境自检：任何启动问题先跑这个（退出码 0=通过）
python -m pytest tests/                # 离线回归：不触网、零 API 成本，必须全绿
BUTLER_RUN_REAL_API_TESTS=1 python -m pytest tests/test_integration_real_api.py   # 真实 API 集成（默认跳过）
python -m butler.emulator --port 8123  # HA REST API 本地模拟器（无 HA 也能联调适配器）
```

- CI：GitHub Actions（`.github/workflows/ci.yml`）——push main/pi_ceshi 与 PR main 时
  Python 3.11 跑 `pytest -q` 离线套件。
- 前端无构建步骤：`butler/web/`（index.html + static/app.js + styles.css）是纯静态文件，直接改。

## 5. 架构与一次请求的生命周期

```
交互层   CLI(cli.py) / Web(webapp.py + web/静态前端) / 语音(voice/)
            │ 三入口共用同一套 Agent/工具/确认机制，语音不绕过确认
            ▼
Agent 循环(butler/agent.py)  ←→  LLM(llm_client.py，OpenAI 兼容，可换厂商)
            │ 每轮：刷新系统提示词 → LLM.chat → 有 tool_calls 则逐个交执行器 → 回传 → 循环
            │ 上限：单轮 8 次(MAX_TOOL_CALLS_PER_TURN)、单响应 4 次(超出回填 error/cancelled)
            │ 收敛：达上限注入"禁止再调工具"做一次无工具最终回答；LLMError → 可读失败回复
            ▼
安全执行器(butler/tools/executor.py)   ←── 程序强制，独立于提示词
            ▼
工具层(tools/)  →  设备适配层(devices/) → simulated / homeassistant / gpio 三后端
侧挂：memory/(SQLite 记忆) · planner.py(多步任务) · routines.py(例行任务)
      proactive.py(规则引擎) · scheduler.py(后台调度) · household/(食材+衣柜)
```

一次用户输入：`Conversation 记录 → Agent 循环 → 工具结果以 role=tool 回传 → 文本回复`。
控制类工具 handler 的固定套路是 **预检查 → 执行 → 回读验证**（`butler/tools/home.py:238-282`，
含异步窗帘 opening/closing 语义）。

## 6. 代码地图（`butler/` 模块速查）

| 模块 | 职责 | 关键点 |
|---|---|---|
| `cli.py` | CLI 入口与装配 | `main()` 装配链 → `_build_agent()` 返回 **5 元组** `(agent, rule_engine, executor, routine_runner, household_checker)`（`butler/cli.py:238`）；UTF-8 reconfigure 兼容 Windows GBK（cli.py:46-54） |
| `webapp.py` | FastAPI 后端（约 1300 行） | 静态前端 `_WEB_DIR=web/`；多令牌鉴权中间件（webapp.py:346-385）；SSE `EventHub`（82-117）；`PairingManager`（120-163）；直控 API（522-639） |
| `web/` | PWA 静态前端 | 5 页签：设备直控/管家对话/例行与提醒/记忆与台账/审计与多端；`EventSource('/api/events?token=...')` |
| `agent.py` | Agent 循环（158 行） | 系统提示词 `build_system_prompt`（35-70）含 5 条硬规则（禁编造传感器数据/拒绝后不重试/只用已注册工具/记忆按原意/简体中文），支持 member 与 persona 注入 |
| `planner.py` | 多步任务 `PlanRunner` | LLM 出 JSON 计划 → 一次自纠重试 → **服务端整体验证**（≤8 步、工具已注册、过 Schema、高风险禁入）→ 顺序执行 → 出错/拒绝/unknown 即中止 |
| `routines.py` | 预授权例行任务 | 创建时确认完整动作清单=授权，执行时不再逐动作确认；≤10 动作；逐动作预检值域；`last_fired_at` 日期守卫每天至多一次 |
| `proactive.py` | 主动规则引擎（509 行） | **纯通知，结构上无执行器句柄——不可能控制设备**。三种规则：sensor_threshold（连击 2 次+冷却 300s）/ device_offline / schedule（HH:MM，错过 30 分钟内补跑） |
| `scheduler.py` | 后台调度 daemon | 默认 60s 一轮（下限 10s）：规则检查 + 例行任务 run_due + 家庭巡检（chain_ticks）；通知交 sink 回调 |
| `token_budget.py` | 每日 token 预算 | 持久化在 memory.db `token_usage` 表，重启不清零；0=不限；超限抛 LLMError 优雅收敛 |
| `llm_client.py` | LLM 适配层 | OpenAI 兼容；多模型档案 `build_llm(config, "main"/"planner", ...)`；token 预算挂载在 chat 前检查/后入账 |
| `conversation.py` | 上下文 | 按整轮裁剪（默认保留 60 条消息） |
| `confirmation.py` | 确认协议 | ConsoleConfirmer / Web 队列 / AutoApprove / AutoDeny 四实现；`CONFIRMATION_MODE=never` 仅调试 |
| `config.py` | 配置加载（292 行） | **必填项只有 `LLM_API_KEY`**；fail-fast（HA 后端必须有 HA_TOKEN、GPIO 后端必须有设备清单文件）；见 §8 |
| `memory/store.py` | SQLite `data/memory.db` | 9 张表：memories / task_logs / proactive_rules / notifications / audit_log（上限 2000 条自动裁剪）/ token_usage / routines / pairings（只存 SHA-256 哈希）/ settings（persona）。WAL+RLock。记忆三档作用域：本人+共享 / 陌生人只看共享 / 管理全量 |
| `devices/base.py` | 设备抽象 | 8 类型常量（sensor/light/fan/switch/cover/climate/humidifier/vacuum）；异常 `DeviceError/DeviceNotFound/DeviceOffline/StateUnknownError` |
| `devices/simulated.py` | 模拟后端（默认） | JSON 状态持久化 `data/simulated_state.json`；默认 4 台设备：sensor.living_room、light.living_room、light.bedroom、fan.bedroom |
| `devices/homeassistant.py` | HA REST 适配器（854 行） | httpx 连接池 16、`trust_env=False` 绝不走代理；传输失败读→DeviceOffline、控制→StateUnknownError；能力位标志镜像 HA EntityFeature，单一事实源 `supported_actions_for`（267 行） |
| `devices/discovery.py` | 自动发现 | 枚举 `/api/states` 按域归类，能力从 supported_features 推导；周期刷新默认 300s（下限 60s）+ WS 事件去抖 2s；变更落审计；HA 不可达保留上次清单；`DISCOVERY_ENABLED=0` 回退白名单 `config/ha_devices.json` |
| `devices/ha_ws.py` | HA WebSocket | 订阅 state_changed 秒级感知；指数退避重连封顶 30s；`auth_invalid` 停止重试；`fetch_area_map()` 提供区域归属 |
| `devices/gpio.py` | 树莓派直连（578 行） | 仅 Pi 可跑（gpiozero 延迟导入）；sensor(dht11/dht22/ds18b20/motion)/light/fan/switch 四类；BCM 引脚 2-27；上电安全默认全关；清单 `config/gpio_devices.json` 空清单拒绝启动 |
| `tools/executor.py` | 安全执行器（332 行） | 流水线见 §7.1；防重放（medium 专用，3 秒窗口，键=工具名+排序 JSON 参数）；只读重试 2 次（0.5/1.0s），**非 LOW 风险绝不自动重试**；超时执行（ThreadPool 4 workers）；`execute_direct()`=直控入口（跳过确认、保留全部校验/风控/审计） |
| `tools/home.py` | 设备工具 4 个 | list_devices / get_device_state / get_sensor_readings（LOW）+ control_device（MEDIUM，13 种动作枚举，超时 15s） |
| `tools/memory_tools.py` | 记忆工具 5 个 | save/update/delete（MEDIUM）+ list/export（LOW） |
| `voice/` | 语音子系统（7 文件） | 见 §7.5 |
| `household/` | 食材+衣柜管家 | 独立 SQLite `data/household.db`（5 张表）；见 §7.6 |
| `emulator/ha.py` | HA 模拟器 | 3 个端点（/api/states 等）+11 台默认实体，与真实 HA 同语义，切真实 HA 只改 `.env` |

## 7. 关键机制要点

### 7.1 安全执行流水线（`butler/tools/executor.py:3-12` docstring，逐段实现在 163-238）

```
注册检查 → JSON 解析 → Schema 校验 → 风险等级 → 防重放 → 用户确认
→ 带超时执行（只读自动重试）→ 结果阶段标记 → 审计日志
```

每步经 `_emit()` 发流水线事件（trace_id 10 位 hex）——这就是 Web 端 SSE「安全流水线」的数据源。
风险三级（`tools/base.py:13-15`）：`low`=只读自动放行可重试；`medium`=控制类必须确认、绝不自动重试；
`high`=默认禁止（执行器直接拒绝，executor.py:191-198）。

### 7.2 设备三后端（`DEVICE_BACKEND` 环境变量切换，Agent 层零改动）

- `simulated`（默认，安全零外部依赖）｜ `homeassistant`（真实设备，REST+WS+自动发现+熔断）｜
  `gpio`（树莓派 40-pin 直连，确认制/审计/回读与其他后端完全一致）。
- 换后端只改 `.env`；新增设备类型 = base.py 加类型常量 + 适配器（实现 `supported_actions/apply`）
  + discovery 域映射（`_DOMAIN_ADAPTERS`）+ 白名单示例更新 + 离线测试。

### 7.3 HA 可靠性三件套（V2 完成）

- **熔断器** `CircuitBreaker`（`devices/homeassistant.py:58-136`）：连续 3 次传输层失败转 OPEN
  快速失败，30s 后 HALF_OPEN 放行单探测；401/404 等业务错误不触发熔断；状态暴露给
  `/api/overview` 与 `/api/system/info`。
- **WebSocket 订阅**（`ha_ws.py`）：状态变更秒级触发发现刷新（去抖 2s），断线指数退避，
  令牌无效停止重试；任一环失败只降级不影响主流程。
- **自动发现**（`discovery.py`）：动态枚举替代静态白名单（`DISCOVERY_ENABLED=0` 可回退），
  高风险域结构性排除（§2 红线 1）。

### 7.4 后台任务分工（勿混淆）

| 组件 | 干什么 | 绝不干什么 |
|---|---|---|
| `proactive.py` 规则 | 阈值/离线/定时**提醒**（纯通知） | 不碰设备（无执行器句柄） |
| `routines.py` 例行任务 | 到点**真实执行**预授权动作清单 | 不执行未经创建确认的动作 |
| `scheduler.py` | 60s 一轮把上面两者+家庭巡检串起来 | 自身无业务逻辑 |
| `planner.py` | 对话中 `/task` 多步任务（逐次确认） | 高风险步骤验证期整单拒绝 |

### 7.5 语音栈（`butler/voice/`）

- TTS：`TTS_BACKEND=sapi`（Windows SAPI5 / Linux espeak，默认）或 `cosyvoice`
  （→ `FallbackSpeaker` 主备降级，降级必留痕）。CosyVoice 服务端**独立 Python 3.10 环境**
  （主项目仅 HTTP 客户端），端口 9880，采样率 24000，模型 Fun-CosyVoice3-0.5B；
  音色克隆需说话人授权（consent 门禁在合成层强制）。
- ASR：faster-whisper small/int8/中文，本地离线；麦克风 16kHz、静音 1.2s 断句、最长 12s。
- 唤醒词：sherpa-onnx 离线 KWS，默认「小管家」，模型约 33MB 在 `data/models/sherpa-kws`；
  换唤醒词改 keywords.txt 无需训练；阈值 0.25。
- 声纹识别（阶段 13）：CosyVoice 服务端 `/speaker_embed` 提取 192 维向量（存 JSON、不留音频），
  余弦相似度阈值 **0.30**（`config.py:274-276`）；只做个性化不做身份验证，确认流程不放松；
  陌生人只看共享记忆。
- 语音层任何异常不得中断 Agent 主流程（播报全经 `_speak_safely` 收敛）。

### 7.6 家庭生活服务（阶段 14.7，`butler/household/`）

- 食材管家：批量入库（≤20 项，MEDIUM 确认）、按 10 类目缺省保质期（保守取短，
  `pantry.py:25-28`）、临期（2 天）过期每日巡检（每项每天至多一次）。
- 衣柜管家：按成员记账（显式指定 > 声纹成员 > 默认「我」）、同日穿着幂等、
  洗涤策略按类目（`wardrobe.py:28-39`）、穿搭推荐=确定性数据聚合（Open-Meteo 免密钥天气，
  坐标留空=关闭）+模型只组织语言不编造。
- 巡检挂在 `scheduler` 的 extra_tick 上，通知走统一通道 `MemoryStore.add_notification`。

### 7.7 V2 新增 API（为 App/小程序准备，`docs/APP_DESIGN.md` §2/§4）

- **多令牌配对**：`POST /api/pair/start`（仅主令牌）生成 6 位配对码（TTL 600s / 3 次尝试 /
  一次性）→ `POST /api/pair/confirm`（公开路径）换设备令牌 `bh_` 前缀，落库只存 SHA-256 哈希。
- **鉴权**：`X-Butler-Token` 头 / `?token=` / `Bearer` 三种携带；主令牌 actor=`user`，
  配对令牌 actor=`device_app:{id}`；公开路径仅 `{/, /manifest.json, /api/health, /api/pair/confirm}`。
- **设备直控**（不经 AI）：`POST /api/devices/{device_id}/command` → `executor.execute_direct()`
  ——跳过确认但保留校验/风控/防重放/回读/审计（actor=`web_direct`）。
- **SSE**：`/api/events` 推送 `confirmation_pending / confirmation_resolved / pipeline_step /
  device_updated / connected`（15s 心跳）。

## 8. 配置速查（`butler/config.py` 292 行，fail-fast）

- **唯一必填**：`LLM_API_KEY`（缺了报 ConfigError 带修复指引）。`.env` 用 python-dotenv 加载
  （`override=False`，真实环境变量优先）。
- fail-fast：`DEVICE_BACKEND=homeassistant` 必须已有 `HA_TOKEN` 且 `HA_URL` 合法；
  `DEVICE_BACKEND=gpio` 必须有设备清单文件——**绝不带默认值裸跑**。
- `.env.example` 分 10 组：大模型 / 运行参数（工具上限 8/4、token 预算 500000/天、历史 60 条、
  确认模式 always）/ WEB_TOKEN（留空=每次启动随机生成打印）/ 设备后端 / GPIO / 主动服务 /
  TTS / 声纹 / 唤醒词 / 天气 / 多模型档案（`LLM_<角色>_*` 逐字段回退全局，main=Agent、
  planner=PlanRunner）。
- ⚠️ `DISCOVERY_ENABLED` / `DISCOVERY_REFRESH_SECONDS` 只在 config.py 支持，未写入
  .env.example——给用户调这个开关时要手动加。

## 9. 测试纪律（DoD：验收实测 + 文档同步 + pytest 全绿 + 清晰提交）

- **一切测试离线**：LLM 用 `tests/helpers.py` 的 `FakeLLM` 脚本化替身；HA 用 monkeypatch
  httpx 或 `butler.emulator`；确认用 `AutoApproveConfirmer`；音频用注入的假检测器。
  新功能必须带离线测试，禁止写触网测试（唯一例外 `test_integration_real_api.py`，默认跳过）。
- `tests/conftest.py` 提供 `store` / `registry` / `tool_registry` 三个 fixture；
  根目录 `conftest.py` 只做 helpers 导入桥接。
- V2 机制的测试范例在 `tests/test_v2_upgrades.py`（熔断开合、直控豁免确认但审计、
  配对生命周期、人设注入、流水线事件）。

## 10. 在本仓库改代码的守则

1. **先跑 `pytest -q` 确认全绿再动手**；改完必须仍全绿。
2. 新增工具的标准清单：`ToolSpec`（正确的 risk 等级 + 参数 JSON Schema + 超时）→ 在对应
   `build_*_tools` 注册 → handler 遵循「预检查→执行→回读」→ 离线测试 → README/ROADMAP/本文件同步。
3. 新增设备类型：见 §7.2 清单；**先查 §2 红线 1 的排除表**，高风险域永远不接。
4. 可选功能 = 延迟导入 + 缺依赖自动降级（参照 gpiozero、sherpa-onnx、faster-whisper 的写法），
   不往 `requirements.txt` 加必需重依赖。
5. 代码风格：中文 docstring/注释、模块头写清职责与安全设计、fail-fast 配置校验、
   异常收敛到可读回复（不裸抛给用户）。跟随现有文件的密度与命名习惯。
6. 文件编码 UTF-8 无 BOM；**PowerShell 脚本（.ps1）必须带 BOM**——PS 5.1 会把无 BOM 的
   中文注释当 ANSI 读导致解析崩溃（历史事故 commit `5bc9a33`）。
7. 不做无关重构/改名；换后端不动 Agent 层是本项目的验收底线。
8. 提交前：`git diff` 自查密钥与调试代码；commit message 用中文、说明动机（参照 `git log` 风格）。

## 11. 已知坑（历史事故与边界，避免重踩）

- **Windows GBK 终端**：CLI 靠 `_force_utf8_stdio()`（cli.py:46-54）保 UTF-8，别删。
- **GPIO 后端只能在树莓派跑**：gpiozero/lgpio 已随 Pi OS 预装，PC 上装/跑会报错。
- **端口占用**：Web 8080 / HA 8123 / CosyVoice 9880 / **模拟器默认也是 8123**——
  与真实 HA 并存时要 `--port` 错开。
- **`butler/desktop/` 是残留空目录**（只有旧 `__pycache__`），不是现役模块，别在里面加东西。
- `butler/web/` 是无构建静态前端，改完刷新即生效；`webapp.py` 改动需重启 uvicorn。
- `audit_log` 上限 2000 条自动裁剪；`household.db` 与 `memory.db` 是两个库，别混。
- CosyVoice 模型 ID 是 **FunAudioLLM** 开头（不是 iic）；换模型时采样率以模型目录
  `cosyvoice*.yaml` 为准；CosyVoice3 需要固定 prompt 前缀（CosyVoice2 要置空）。
- HA 宕机不再卡死（熔断器已修），但熔断器只管**传输层**错误——401（令牌过期）要去看
  RUNBOOK §8 重新生成令牌。
- ESP32 固件已通过 ESPHome 2026.10.1 校验但**未真机烧录**；旧编译产物基于旧实体名，
  插线后 `esphome run` 会自动重编译（`docs/HARDWARE.md` §4）。
- **跨源 401 必须带 CORS 头**（2026-11-21 修复）：`/api/*` 的 401 从最外层鉴权中间件直接
  返回、不经过 CORSMiddleware；缺头时浏览器会把 401 吞成网络错误，H5 端 App 无法触发
  "凭证失效→重新配对"收敛。回归测试
  `tests/test_webapp.py::test_unauthorized_response_carries_cors_headers`。

## 12. 文档索引（docs/ 共 15 篇，按需深入）

| 文档 | 用途 |
|---|---|
| `REQUIREMENTS.md` | 需求与验收标准 |
| `ARCHITECTURE.md` | 分层架构、数据流、决策取舍（最核心的设计文档） |
| `TECH_STACK_DECISIONS.md` / `OPEN_SOURCE_REUSE.md` | 选型依据 / 开源复用记录 |
| `RUNBOOK.md` | 启动/自检/HA 部署/日志/备份/升级/故障排查 |
| `ROADMAP.md` | 阶段表 + 近期待办（⚠️ 2026-11-01 后部分滞后，见 §3） |
| `HARDWARE.md` / `PI_GPIO_TESTING.md` | ESP32 接入 / GPIO 接线表+安全红线 |
| `PI_DEPLOYMENT.md` + `scripts/pi/README.md` | 树莓派部署手册（未真机验证） |
| `VOICE.md` / `COSYVOICE_SETUP.md` | 语音栈 / CosyVoice 独立环境部署 |
| `APP_DESIGN.md` | 应用软件 V2 设计（配对制/直控/云中转/阶段 15-18） |
| `MOBILE_APP_SPEC.md` | PE 端移动 App 完整设计（页面交互/API 契约/实时通信/验收清单，对应 `aihome-butler-mobile/`） |
| `GRADUATION_PROPOSAL.md` | 毕业设计开题报告 |
| `REPO_STRUCTURE.md` | 目录级结构总览（2026-11-15，pi_ceshi 视角） |

## 13. 运行时数据与部署拓扑（不进 git 的部分）

- `data/`：`memory.db`（记忆/任务/规则/通知/审计/配对）、`household.db`（家庭台账）、
  `simulated_state.json`、声纹/音色档案、唤醒词模型。`logs/butler.log`：UTF-8 1MB×3 滚动，
  不含密钥，Web 不落对话正文。
- 部署拓扑目标态：**树莓派 = 管家 + HA + 声纹一体机**（systemd 服务 `butler` 开机自启）；
  阿里云 = 穿透/ntfy 推送枢纽（阶段 18）；CosyVoice（3GB 模型）留在 PC 跨机调用。
  PC 开发用 `scripts/backup.ps1`，Pi 用 `scripts/backup.sh`（各保留最近 10 份）。
