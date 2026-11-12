# AI Home Butler（AI 智能家居管家）

在成熟开源智能家居基础设施之上构建的 AI 家庭管家：具备环境理解、记忆、任务规划、
安全执行与主动服务能力。**不重复造 Home Assistant，不重写语音引擎。**

许可证：[MIT](LICENSE) ｜ 当前状态：**阶段 0-14 首轮全部完成** —— 文本/网页/语音三种交互，
模拟与 Home Assistant 双设备后端（真实 HA 容器已部署运行），长期记忆，多步骤任务规划，
定时提醒，预授权例行任务，主动提醒，语音播报，成员声纹识别，唤醒词待机，
食材管家与衣柜管家（阶段 14.7）；
ESP32 烧录差一步用户操作（见 docs/ROADMAP.md「近期待办」）。

- 需求与验收：[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)
- 架构设计：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 技术选型：[docs/TECH_STACK_DECISIONS.md](docs/TECH_STACK_DECISIONS.md)
- 开源复用记录：[docs/OPEN_SOURCE_REUSE.md](docs/OPEN_SOURCE_REUSE.md)
- 运行维护手册：[docs/RUNBOOK.md](docs/RUNBOOK.md)
- 硬件接入：[docs/HARDWARE.md](docs/HARDWARE.md) ｜ 语音：[docs/VOICE.md](docs/VOICE.md)
- 树莓派部署：[docs/PI_DEPLOYMENT.md](docs/PI_DEPLOYMENT.md)（选型/烧录/一键脚本/迁移/验证）
- 路线图：[docs/ROADMAP.md](docs/ROADMAP.md)

## 能做什么

- **文本 CLI**（`python -m butler`）：多轮对话、传感器查询、设备控制（确认制）。
- **Web 界面**（`python -m butler.webapp`）：仪表盘+对话+页面批准确认+记忆管理+任务记录。
- **语音模式**（`python -m butler.voice`，`--mic` 语音输入）：SAPI 离线播报 + 本地 whisper 识别。
- **长期记忆**：用户确认后保存偏好/事实，随时查看/修改/删除/导出，立即影响决策。
- **多步骤任务**：`/task 我准备睡觉了，关掉卧室灯并打开风扇低档` —— 计划先验证再执行，
  逐步可追踪，出错即中止。
- **主动提醒**：对话里说「温度超过 30 度提醒我」即可创建规则（后台每 60 秒自动检查，
  连续命中+冷却防霸屏，纯通知，绝不自动控制设备）。
- **定时提醒**（档 1）：「每天 08:00 提醒爷爷量血压」「工作日 07:20 提醒小星出门上学」
  ——到点自动通知，可指定家庭成员，错过时刻 30 分钟内补跑，每天至多提醒一次。
- **例行任务**（档 2）：「工作日 07:00 自动开客厅窗帘」——创建时确认完整动作清单
  即完成预授权，到点自动执行（无需再次确认）；逐动作预检、执行留审计、失败立即
  通知并中止后续步骤；门锁/热水器等高风险设备类型结构上无法进入例行任务。
- **双设备后端**：`DEVICE_BACKEND=simulated`（默认，安全）或 `homeassistant`（真实设备，
  自动发现+动态能力——运行时枚举 HA 实体、按能力位标志推导每台设备可做的动作，
  高风险设备类型结构性排除，`DISCOVERY_ENABLED=0` 可回退手动白名单），Agent 层零改动切换。
- **家庭成员声纹**（阶段 13）：注册家人声纹后，语音模式自动识别谁在说话——
  称呼个性化、记忆按人隔离（陌生人只看共享记忆）、按成员切换回答音色；
  声纹仅用于个性化，不作为身份验证，确认流程不放松。
- **唤醒词待机**（阶段 14）：`--mic --wake` 说「小管家」唤醒——sherpa-onnx 离线
  关键词检测（3.3M 模型约 33 MB，CPU 约 50 倍实时，树莓派可跑），换唤醒词改
  keywords.txt 即可无需训练；依赖未装自动退回免唤醒模式。见 docs/VOICE.md §6。
- **可选 CosyVoice 中文合成 + 音色库**（阶段 12）：`TTS_BACKEND=cosyvoice` 切换到
  官方开源 CosyVoice（独立环境运行，主项目零新增依赖）；上传参考录音（需说话人
  授权）即可克隆音色，管家用你的声音播报回复；服务故障按配置降级回 SAPI。
  部署见 [docs/COSYVOICE_SETUP.md](docs/COSYVOICE_SETUP.md)。
- **食材管家**（阶段 14.7）：对话里说「今天买了西红柿、鸡蛋和排骨」批量入库，
  「西红柿用完了」登记消耗；台账按入库日期+保质期倒计时，临期/过期由后台巡检
  每天至多提醒一次；「家里能做什么菜」基于真实库存推荐，不编造。
- **衣柜管家**（阶段 14.7）：按成员登记衣物，「今天穿的什么」一句话记账（同日
  重复自动幂等），穿着次数+距上次洗涤天数触发「该洗了」提醒，洗好报一下即重置；
  「今天穿什么」结合 Open-Meteo 天气（`.env` 配坐标即可，免费免密钥）与你的历史
  反馈（"这条裤子显胖"会被记住）给出建议；未配天气时照常推荐并如实说明。

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

### 多模型配置（可选）

支持"一个主模型 + 若干次级模型"：每个角色一组 `LLM_<角色>_*` 变量
（`BASE_URL` / `MODEL` / `API_KEY` / `TIMEOUT_SECONDS` / `MAX_RETRIES`），
未定义的字段自动继承全局 `LLM_*` 配置。内置角色绑定：

- `main` —— 对话主模型（Agent，完整工具集 function calling）
- `planner` —— 多步任务规划（PlanRunner，独立汇报提示词）

一个都不配置时所有角色共用全局模型，行为与单模型完全一致。
换厂商/换模型只改 `.env`，不改代码；新增次级消费者时在装配处
`build_llm(config, "<角色名>")` 绑定即可。

## 运行测试

```bash
python -m pytest tests/            # 离线回归（不触网、零 API 成本）
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
├── voice/            # 语音桥接：SAPI TTS + faster-whisper ASR + 麦克风 + 唤醒词
├── household/        # 家庭生活服务：食材管家 + 衣柜管家（台账/巡检/天气穿搭）
└── tools/            # 工具注册表、参数校验、安全执行器、家庭/记忆/规则工具集
firmware/esphome/     # ESP32 固件配置（ESPHome 2026.10.1 与 2025.5.2 双版本校验通过）
scripts/              # HA 一键部署、备份脚本
tests/                # 327 项测试：离线回归 + 真实 API 集成（默认跳过）
docs/                 # 需求/架构/选型/复用/路线图/RUNBOOK/硬件/语音
```

安全层执行顺序：注册检查 → JSON 解析 → Schema 校验 → 风险等级 → 用户确认 → 执行 →
回读验证 → 审计日志。高风险操作（门锁/燃气/电热类）默认禁止，无注册工具。

## 已知限制

- 设备控制支持 8 类：sensor / light / fan / switch / cover（窗帘）/ climate（空调温控）/
  humidifier / vacuum；HA 白名单外的实体不暴露，lock / water_heater / alarm 等高风险
  域不实现（安全设计）。
- ESP32 待烧录：固件已通过 ESPHome 2026.10.1 校验；此前编译产物（2025.5.2）基于旧实体名，
  插线后 `esphome run firmware/esphome/esp32-butler.yaml` 会自动重新编译并烧录。
- 语音输入的噪声环境表现未做真实调试；无唤醒词（演进方向 HA Voice，见 docs/VOICE.md）。
- `CONFIRMATION_MODE=never` 仅供调试，会跳过所有确认并输出警告日志。
- Web 界面要求访问令牌（.env 设 WEB_TOKEN，留空则每次启动自动生成并打印），
  默认仅绑定 127.0.0.1，勿暴露公网。
- 例行任务的 Web 管理面板待建（API `/api/routines` 与对话工具已可用）。

## 安全提示

- API Key 只保存在本地 `.env`（已在 `.gitignore`），不要提交或分享。
- 若 Key 曾在聊天等场合明文传递，建议到服务商控制台轮换。
