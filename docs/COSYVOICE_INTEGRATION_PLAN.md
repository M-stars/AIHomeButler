# CosyVoice 接入方案（调查与部署决策）

更新日期：2026-10-28 ｜ 状态：方案已确定，实施进度见文末「实施状态」

本文是接入 CosyVoice 前的官方调查与本机部署决策记录。使用说明见
[docs/COSYVOICE_SETUP.md](COSYVOICE_SETUP.md)。

## 1. 官方仓库调查结论

| 调查项 | 结论（2026-10-28 核实） |
|---|---|
| 官方仓库 | https://github.com/QwenAudio/CosyVoice （原 FunAudioLLM/CosyVoice，README 内 clone 命令仍写 FunAudioLLM，两者互为重定向） |
| 推荐模型 | **Fun-CosyVoice3-0.5B-2512**（README 唯一标注"推荐模型"） |
| 模型下载 | ModelScope：`FunAudioLLM/Fun-CosyVoice3-0.5B-2512`（注意组织名不是 iic，实测 `iic/` 下无此模型）；HuggingFace：`FunAudioLLM/Fun-CosyVoice3-0.5B-2512` |
| 模型体积 | HF 主分支总大小约 **4.86 GB**（safetensors 权重 + llm.onnx + 配置；磁盘占用另加下载缓存，见 §4） |
| 模型许可证 | **Apache-2.0**（HF 模型页标注；备选模型 CosyVoice2-0.5B 同为 Apache-2.0，约 4.85 GB） |
| 代码许可证 | 仓库源码 **Apache-2.0**（含 runtime/python/fastapi 官方服务端文件头声明） |
| Python | 官方安装说明使用 **conda + python 3.10** |
| 核心依赖 | torch/torchaudio、`requirements.txt`（conformer、diffusers、HyperPyYAML、librosa、lightning、modelscope、pyworld、wetext、x-transformers、grpcio、onnxruntime 等，共 28 项 + 2 个额外索引） |
| CUDA | 官方 requirements 以 cu121 索引为主；本机驱动为 CUDA 13.0，向下兼容 cu121/cu124 运行时 |
| Windows 原生 | **官方未提供 Windows 说明**；Linux 独有组件为 sox（音频后端，可用 torchaudio backend 绕过）与 ttsfrd 文本正则化 wheel（**官方已声明：不装 ttsfrd 时默认使用 wetext**，wetext 为纯 Python 包可装于 Windows）。依赖逐项 Windows 可安装性核查见 §3 |
| 推理接口 | 官方 `example.py`：`AutoModel(model_dir=...)` + `inference_sft / inference_zero_shot / inference_cross_lingual / inference_instruct2`，零样本参数 = 目标文本 + 参考文本 + 16k 参考音频；`inference_zero_shot(..., stream=False)` 返回音频 chunk 生成器 |
| 官方服务端 | **有**：`runtime/python/fastapi/server.py`（FastAPI + grpc 双实现，fastapi 版自 2024/07 进入主分支）。gRPC 版需要额外编译 proto 并占 50001 端口 |
| 模型加载 | `AutoModel` / `CosyVoice2` / `CosyVoice3` 类；仓库以 git submodule 引入 third_party/Matcha-TTS，运行时需 `sys.path.append('third_party/Matcha-TTS')` |

放弃的路线与原因：

- **gRPC 官方服务端**：需要额外 `grpcio-tools` 编译 proto、监听 50001 端口、返回
  自定义二进制流格式；FastAPI 版 API（POST /api/tts 表单 + WAV 流响应）已完全满足
  需求且更易做健康检查。选 **FastAPI 官方服务端**。
- **WebUI（gradio）**：交互式演示界面，不适合被管家程序化调用。
- **Docker**：本机 Docker Desktop 已安装但守护进程未运行，且 Windows 下 GPU 直通
  依赖 WSL2 后端，本机 WSL2 未安装（`wsl --status` 报"未安装"）。不装 WSL2 的前提下
  Docker 路线只能得到 CPU 推理。放弃（不擅自安装 WSL2/重启电脑）。
- **Conda**：本机未安装 conda/miniconda。放弃（同理，不擅自装系统级组件）。

## 2. 本机环境实测（2026-10-28）

| 项目 | 实测值 | 对部署的影响 |
|---|---|---|
| CPU | Intel i7-12650H（12 核） | CPU 推理可用但慢（估 RTF > 1） |
| GPU | NVIDIA GeForce RTX 4060 Laptop，**8 GB 显存**，驱动 581.80（CUDA 13.0） | 0.5B 模型 GPU 推理显存足够（权重约 2-3 GB + 运行时开销） |
| 内存 | 15.7 GB | 足够（模型加载峰值约 4-6 GB） |
| 磁盘 | D: 剩余 183 GB；C: 剩余 36 GB | 足够 5 GB 模型 + 约 6-8 GB 依赖环境 |
| 主环境 | Python 3.9.13（D:\PythonInstall），含 torch 2.8.0+cu126（cuda available=True） | 主环境**不装** CosyVoice 任何新依赖（原则 3） |
| conda | 未安装 | 不用 |
| Docker | 29.9.2 已装，守护进程未运行，WSL2 缺失 | 不用 |
| WSL2 | 未安装 | 不用 |
| **uv** | **0.13.5 已安装** | ✅ 用它创建隔离环境（见 §4） |

测试基线：`python -m pytest tests/` → **120 passed, 2 skipped**（6.4 s，离线）。

## 3. 依赖 Windows 可安装性核查

对官方 requirements.txt 逐项核查 PyPI 预编译 wheel（Python 3.10 win_amd64）：

- **有 Windows wheel / 纯 Python，可直接安装**：conformer、diffusers、HyperPyYAML、
  librosa、lightning、modelscope、openai-whisper、grpcio 1.57、onnxruntime、
  matcha-tts（PyPI 有同包）、numpy、requests、torchaudio、tqdm、x-transformers、
  wetext（纯 Python）。其中 pyworld 提供官方 win_amd64 wheel（无需编译）。
- **deepspeed**：官方 requirements 的 Windows 安装分支里，deepspeed 仅在
  `sys_platform == 'linux'` marker 下安装（推理不需要它），Windows 不会拉起。
- **wetext**（替代 ttsfrd 的文本正则化）：官方 README 明确"不装 ttsfrd 时默认用
  wetext"，即官方认可的 Windows 替代路径。
- **结论**：Windows 原生运行官方推理代码可行，无需修改官方核心代码。

## 4. 隔离环境决策

**选定方案：uv 管理的独立 venv（Python 3.10）+ 官方 FastAPI 服务端。**

```
AI Home Butler（Python 3.9 主环境，零新增依赖）
      │  HTTP（127.0.0.1:9880）
      ▼
官方 runtime/python/fastapi/server.py（薄适配层但ler侧自写，服务端用官方原版）
      │  同机
      ▼
uv venv：D:\AIHomeButler\cosyvoice\.venv（Python 3.10 + 官方 requirements）
      │  sys.path.append(third_party/Matcha-TTS)
      ▼
官方 CosyVoice 代码（git clone 官方仓库，含 submodule）+ 模型权重（本地目录，不入 Git）
```

目录布局（全部加入 .gitignore，不入仓库）：

```
D:\AIHomeButler\cosyvoice\
├── CosyVoice\          # 官方仓库 clone（git submodule 含 Matcha-TTS）
├── .venv\              # uv 创建的 Python 3.10 独立环境
└── pretrained_models\Fun-CosyVoice3-0.5B\   # 模型权重（约 4.9 GB）
```

- 不污染主环境：主项目 Python 3.9 只新增**标准库 + 已有依赖**（httpx/fastapi 已在
  requirements.txt），CosyVoice 客户端用 httpx 实现。
- 模型权重放 D 盘（剩余 183 GB），绝不入 Git；`cosyvoice/` 整目录进 .gitignore。
- 与 conda 方案对比：uv 装的是用户级 Python 解释器（%LOCALAPPDATA% 下，无注册表、
  无管理员权限、不进系统 PATH），删除 .venv 即完全卸载，可逆性最好。

### 服务端选择：官方 FastAPI server.py 原样使用

官方 `runtime/python/fastapi/server.py` 提供：

- `POST /api/tts`：表单参数 `text`（必填）、`prompt_text`、`prompt_audio`（文件上传）、
  `instruct_text`、`stream`；返回 WAV 音频流（StreamingResponse）。
- 健康检查：官方文件**没有**专门 health 端点 —— 主项目以 `GET /docs`（FastAPI 自带
  Swagger UI）可达性 + 200 状态作为健康判据，并在客户端层对 /api/tts 失败做
  明确错误分类。不改官方代码，不新增 fork。

**实测修正（2026-10-28）**：官方 FastAPI server 与 CosyVoice3 前端不兼容——
官方路由把上传音频转成张量传给 `inference_zero_shot`，而 Fun-CosyVoice3 前端
要求文件路径（内部自行 load_wav），传张量必然 TypeError。按"官方服务端不适用
时编写最薄适配层"原则，`scripts/cosyvoice_server.py` 复用官方 AutoModel 与
推理代码、仅重写三个路由（表单与 PCM 流格式与官方一致），并把监听地址从官方
默认 0.0.0.0 收敛为 127.0.0.1。官方文件零修改。另发现：Fun-CosyVoice3-0.5B
模型无 spk2info.pt（无预设音色），inference_sft 对该模型不可用，克隆一律走
zero-shot。文本长度限制与并发控制在主项目客户端层实现。

## 5. 主项目集成设计（概要，详见 docs/COSYVOICE_SETUP.md）

- 新增 `TTS_BACKEND=sapi|cosyvoice`（默认 sapi，一切现有行为不变）；
  CosyVoice 相关配置统一 `COSYVOICE_*` 前缀。
- `butler/voice/tts.py` 扩展：沿用现有 `Speaker` 抽象（speak / speak_to_file），
  新增 `CosyVoiceSpeaker`（httpx 调官方服务端）+ 配置化降级包装 `FallbackSpeaker`。
- 音色档案：`data/voices/`（JSON 档案 + 参考录音），含 consent 授权字段；
  未经授权的档案拒绝启用克隆合成（详见文档）。
- Web 仪表盘复用现有卡片结构，新增"语音设置"卡片。
- 失败降级：cosyvoice 失败 → 按配置降级 sapi 并在日志/界面标注，绝不静默换声。

## 6. 已知限制（如实记录）

1. 官方仓库与模型说明均无 Windows 原生支持承诺；本方案依赖 wetext 替代 ttsfrd、
   torchaudio 内置后端替代 sox，音频结果理论上无差异，但属"官方未测路径"。
2. Fun-CosyVoice3-0.5B-2512 的模型 README 未包含"克隆他人声音需授权"的强制条款，
   本项目在产品层自行加授权约束（consent 字段 + 界面声明），不依赖模型方约束。
3. 8 GB 显存运行 0.5B 模型实测可行（占用约 3.6 GB，RTF≈1.08 非流式）；若 OOM，回退 CosyVoice2-0.5B（同为
   Apache-2.0）或 CPU 模式，均写入 SETUP 文档。
4. 官方 FastAPI server 无鉴权与健康检查端点，只允许 127.0.0.1 绑定。
5. 流式推理（官方支持）第一版不实现，先保证非流式稳定（任务书允许）。

## 7. 实施状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| A 调查与基线 | 本文档 §1-§4 + 测试基线 120 passed / 2 skipped | ✅ 完成 |
| B 部署方案确定 | 本文 §4 | ✅ 完成 |
| C 独立语音服务 | uv 隔离环境 + 模型下载 + 服务启动 + 真实中文合成（RTF≈1.08）| ✅ 完成（含官方 server 不兼容的实测修正） |
| D 接入主项目 | TTS 适配层 / 配置 / 音色档案 / Web / 降级 | ✅ 完成（端到端实测） |
| E 测试与安全 | 新增 51 项 Mock 测试 + 全量回归 171 通过 / 2 跳过 | ✅ 完成 |
| F 交付 | 文档 + 启动脚本 + 交付报告 | ✅ 完成（音质人工试听待用户确认） |
