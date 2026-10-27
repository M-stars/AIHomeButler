# 语音交互方案（阶段 8；阶段 12 扩展 CosyVoice 后端）

更新日期：2026-10-28

## 1. 策略

长期方案：**复用 Home Assistant Voice / Wyoming 生态**（官方维护、本地优先、隐私友好），
本项目绝不自行实现语音识别或合成引擎。在此之前，本仓库提供一个 Windows 桥接实现，
让"说话 → 管家 → 回答"的闭环先跑起来，并与文本模式共用全部安全机制。

阶段 12 引入 **CosyVoice 官方开源项目**作为可选 TTS 后端：中文合成质量显著优于
SAPI，并支持**参考音频零样本音色克隆**——用户提交自己（或已获授权说话人）的
参考录音后，管家可以用同样的音色播报回复；多个音色档案构成"音色库"。
部署方式与排查见 [COSYVOICE_SETUP.md](COSYVOICE_SETUP.md)。

## 2. 当前实现（butler/voice/）

| 环节 | 实现 | 状态 |
|---|---|---|
| 语音输出 TTS（默认） | pyttsx3 → Windows SAPI5（离线，无需网络） | ✅ 已验证（测试合成真实 wav 文件） |
| 语音输出 TTS（可选） | CosyVoice 官方服务端（独立 Python 3.10 环境，HTTP 127.0.0.1） | ✅ 阶段 12（见 SETUP 文档实测记录） |
| 音色库 | VoiceProfileManager：档案 JSON + 本地参考录音 + 授权门禁（未授权不可克隆） | ✅ 阶段 12 |
| 失败降级 | FallbackSpeaker：CosyVoice 故障按配置降级 SAPI，降级必留痕（日志+界面） | ✅ 阶段 12 |
| 语音识别 ASR | faster-whisper 本地识别（CPU int8，small 模型） | ✅ 已安装可用（首次运行需下载模型） |
| 麦克风采集 | sounddevice + 静音检测自动断句 | ✅ 已安装（真实录音需设备在手验证） |
| 唤醒词 | 未实现 | ⬜ 留给 HA Voice 方案 |

所有可选依赖均为延迟导入：未安装不影响系统其余功能，并给出可操作的错误提示。

## 3. 使用

```bash
python -m butler.voice             # 键盘输入 + 语音播报（无额外依赖要求）
python -m butler.voice --mic       # 麦克风说话 + 本地识别（已具备依赖）
python -m butler.voice --wav x.wav --mic   # 识别单个 wav 文件
python -m butler.voice --no-speak  # 调试：关闭播报
```

TTS 后端由 .env 的 `TTS_BACKEND` 决定（`sapi` 默认 / `cosyvoice`），
后端选择、音色库管理、试听与默认音色设置在 Web 界面「语音设置（音色库）」
卡片完成（音色库功能需 CosyVoice 服务在线）。

安全约定：**语音指令与文本指令走同一个 Agent、同一套工具确认**。控制类操作
（开灯等）在终端请求 y/N 确认；高风险操作不会因语音而绕过任何检查（阶段 8 验收条件）。

## 4. 语音链路职责分离（阶段 12 固化）

```
ASR（听懂用户） → Agent（理解/生成回复/调用工具，安全确认流程不变）
              → TTS（只负责把最终回复文本变成音频，不改写内容）
              → 音色管理（决定用哪个声音） → 播放（winsound/浏览器播放）
```

- TTS 不接触 Home Assistant、ESP32、数据库或设备接口，不绕开任何权限检查；
- Agent 回复内容在送合成前不被修改（音频生成模块只做文本→语音）；
- CosyVoice 不可用时按 `COSYVOICE_FALLBACK_SAPI` 降级并在日志/界面说明，
  绝不悄悄换声；`0` 配置时直接报错。

## 5. 已知限制

- 麦克风录音的噪声环境表现未做真实调试验证（当前机器无人在旁），`--mic` 首次
  运行会下载 whisper 模型（数百 MB，走代理可能较慢）。
- 无唤醒词：按回车后开始说话的"按键说话"模式。
- 中文识别依赖 whisper small 模型，嘈杂环境准确率有限。
- CosyVoice 为非流式合成（第一版策略：先稳定正确，流式见官方接口后续评估）。
- 克隆音色的相似度取决于参考录音质量（安静环境、5~15 秒、参考文本逐字准确）。

## 6. 演进路线（HA Voice / Wyoming）

1. 部署 Home Assistant 后，安装官方 **Wyoming** 集成与语音服务
   （whisper / piper / openWakeWord 均为官方支持的服务）。
2. 语音终端选项：
   - **Home Assistant Voice Preview Edition** 官方硬件（约 50 美元）；
   - 或现有 ESP32 + INMP441 麦克风自制 Wyoming Satellite（社区成熟方案）。
3. 管家侧接入方式：把 HA Assist 管线的大模型对话指向本项目暴露的对话 API
   （阶段 9 的 Web API），即"HA 管语音前端，Butler 管对话与执行"，两边复用成熟件。
4. CosyVoice 流式推理（官方支持双流式）：待基础链路在本机稳定实测后评估。
