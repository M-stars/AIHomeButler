# 语音交互方案（阶段 8）

更新日期：2026-10-27

## 1. 策略

长期方案：**复用 Home Assistant Voice / Wyoming 生态**（官方维护、本地优先、隐私友好），
本项目绝不自行实现语音识别或合成引擎。在此之前，本仓库提供一个 Windows 桥接实现，
让"说话 → 管家 → 回答"的闭环先跑起来，并与文本模式共用全部安全机制。

## 2. 当前实现（butler/voice/）

| 环节 | 实现 | 状态 |
|---|---|---|
| 语音输出 TTS | pyttsx3 → Windows SAPI5（离线，无需网络） | ✅ 已验证（测试合成真实 wav 文件） |
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

安全约定：**语音指令与文本指令走同一个 Agent、同一套工具确认**。控制类操作
（开灯等）在终端请求 y/N 确认；高风险操作不会因语音而绕过任何检查（阶段 8 验收条件）。

## 4. 已知限制

- 麦克风录音的噪声环境表现未做真实调试验证（当前机器无人在旁），`--mic` 首次
  运行会下载 whisper 模型（数百 MB，走代理可能较慢）。
- 无唤醒词：按回车后开始说话的"按键说话"模式。
- 中文识别依赖 whisper small 模型，嘈杂环境准确率有限。

## 5. 演进路线（HA Voice / Wyoming）

1. 部署 Home Assistant 后，安装官方 **Wyoming** 集成与语音服务
   （whisper / piper / openWakeWord 均为官方支持的服务）。
2. 语音终端选项：
   - **Home Assistant Voice Preview Edition** 官方硬件（约 50 美元）；
   - 或现有 ESP32 + INMP441 麦克风自制 Wyoming Satellite（社区成熟方案）。
3. 管家侧接入方式：把 HA Assist 管线的大模型对话指向本项目暴露的对话 API
   （阶段 9 的 Web API），即"HA 管语音前端，Butler 管对话与执行"，两边复用成熟件。
