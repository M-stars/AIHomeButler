# CosyVoice 部署与使用指南（Windows）

更新日期：2026-10-28 ｜ 适用：AI Home Butler 阶段 12

选型与调查依据见 [COSYVOICE_INTEGRATION_PLAN.md](COSYVOICE_INTEGRATION_PLAN.md)。
本文只讲"怎么装、怎么跑、怎么排错"。

## 1. 官方仓库与模型

- 官方仓库：https://github.com/QwenAudio/CosyVoice （原 FunAudioLLM/CosyVoice，互相重定向）
- 采用模型：**Fun-CosyVoice3-0.5B-2512**（官方推荐；Apache-2.0，代码与权重均已核实）
- 下载源：ModelScope `FunAudioLLM/Fun-CosyVoice3-0.5B-2512`（国内快）
  或 HuggingFace `FunAudioLLM/Fun-CosyVoice3-0.5B-2512`
- 权重体积：约 4.9 GB；存放在 `cosyvoice/pretrained_models/Fun-CosyVoice3-0.5B/`，
  该目录被 .gitignore 覆盖（`cosyvoice/` 整目录），**绝不提交到 Git**。

## 2. 为什么用独立环境

官方 CosyVoice 需要 Python 3.10 + torch 2.3.1 + 约 40 个三方包，而主项目
运行在已验证的 Python 3.9.13。为满足"不污染主环境"的约束（原任务原则 3），
CosyVoice 全部依赖装在 `cosyvoice/.venv`（uv 管理的 Python 3.10 独立虚拟环境）：

- 主项目只通过 HTTP（127.0.0.1:9880）调用服务端，主环境**零新增依赖**
  （客户端用的 httpx 是主项目原有依赖）；
- 隔离环境完全在仓库 `cosyvoice/` 目录内，删除该目录即完全卸载；
- 不需要 conda/管理员权限/系统组件变更（本机无 conda、无 WSL2，uv 为用户级安装）。

## 3. 环境准备（一键脚本）

前置：Windows 10/11、git、uv（`uv --version` 可执行）。全部命令在仓库根目录运行。

```bat
scripts\cosyvoice_setup.bat
```

该脚本等价于以下手工步骤（已在 2026-10-28 本机实际执行成功）：

```bat
git clone --recursive https://github.com/QwenAudio/CosyVoice.git cosyvoice\CosyVoice
uv venv cosyvoice\.venv --python 3.10
uv pip install --python cosyvoice\.venv torch==2.3.1 torchaudio==2.3.1 --index-url https://download.pytorch.org/whl/cu121
rem multi-index（官方 requirements 头两行 extra-index 需要 unsafe-best-match）
findstr /v "openai-whisper" cosyvoice\CosyVoice\requirements.txt > cosyvoice\requirements-without-whisper.txt
uv pip install --python cosyvoice\.venv --index-strategy unsafe-best-match -r cosyvoice\requirements-without-whisper.txt
rem openai-whisper 是 sdist 且 setup.py 用了 pkg_resources（新版 setuptools 已移除）：
rem 必须先在环境内放 setuptools<81 再关闭构建隔离安装——这是本机踩过的唯一安装坑
uv pip install --python cosyvoice\.venv "setuptools<81"
uv pip install --python cosyvoice\.venv --no-build-isolation openai-whisper==20231117
```

验证隔离环境：

```bat
cosyvoice\.venv\Scripts\python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

本机实测输出：`2.3.1+cu121 True`（RTX 4060 Laptop 8GB）。

## 4. 下载模型

```bat
scripts\cosyvoice_download_model.bat
```

等价于：

```bat
cosyvoice\.venv\Scripts\modelscope.exe download --model FunAudioLLM/Fun-CosyVoice3-0.5B-2512 --local_dir cosyvoice\pretrained_models\Fun-CosyVoice3-0.5B
```

> 注意：ModelScope 的组织名是 **FunAudioLLM**（`iic/` 下没有该模型，实测报
> "does not exist"）。下载完成后目录里应有 `cosyvoice3.yaml`、`llm.pt`、
> `flow.pt`、`hift.pt`、`speech_tokenizer_v3.onnx`、`campplus.onnx` 等文件。

## 5. 启动 / 健康检查 / 停止

```bat
scripts\cosyvoice_start.bat     REM 启动（首次加载模型约需数十秒）
scripts\cosyvoice_health.bat    REM 健康检查：HTTP 200 = 就绪
scripts\cosyvoice_stop.bat      REM 按端口结束进程
```

手工等价命令：

```bat
cosyvoice\.venv\Scripts\python.exe scripts\cosyvoice_server.py --host 127.0.0.1 --port 9880 --model_dir cosyvoice\pretrained_models\Fun-CosyVoice3-0.5B
```

- `scripts/cosyvoice_server.py` 是**薄适配层**（两层原因，均为本机实测发现）：
  1. 官方 server 的 `__main__` 固定绑定 0.0.0.0，会暴露到局域网；
  2. **官方 server 与 CosyVoice3 不兼容**：官方路由把上传音频转成张量传入，
     而 Fun-CosyVoice3 前端在 `_extract_speech_feat/_extract_speech_token`
     中要求文件路径（内部自行 load_wav 重采样），传张量必然 TypeError
     （`Invalid file: tensor([[...]] )`，2026-10-28 实测复现）。
  适配层复用官方 `AutoModel` 与官方推理代码（模型加载/合成全部官方原版），
  仅重写三个路由（/inference_sft、/inference_zero_shot、/inference_cross_lingual）：
  表单参数与 PCM 流式响应格式与官方一致，zero-shot 把上传音频落盘为临时文件、
  把路径交给官方推理，流结束后删除。官方文件零修改。
- 健康检查判据：`GET http://127.0.0.1:9880/docs` 返回 200（官方服务端无专门
  health 端点，FastAPI 自带 /docs 页面即探针；主项目客户端同判据）。
- 服务就绪后控制台会打印：模型就绪、采样率、可用预设音色列表。

## 6. 主项目接入配置（.env）

```
TTS_BACKEND=cosyvoice          # sapi（默认）| cosyvoice
COSYVOICE_URL=http://127.0.0.1:9880
COSYVOICE_TIMEOUT_SECONDS=120  # 首次合成/长文本更慢，CPU 推理建议调大
COSYVOICE_FALLBACK_SAPI=1      # 1=服务故障自动降级 SAPI（界面会提示）；0=直接报错
COSYVOICE_MAX_TEXT_CHARS=500
COSYVOICE_SAMPLE_RATE=24000    # 换模型时以模型目录 cosyvoice*.yaml 的 sample_rate 为准
COSYVOICE_PROMPT_PREFIX=You are a helpful assistant.<|endofprompt|>
                               # CosyVoice3 官方 zero-shot 格式前缀；换 CosyVoice2 需置空
```

默认 `TTS_BACKEND=sapi`：一切行为与历史版本完全一致。改完 .env 重启
`python -m butler.webapp` 或 `python -m butler.voice` 生效。

## 7. 音色库使用（克隆你的声音）

Web 界面（`python -m butler.webapp`）→「语音设置（音色库）」卡片：

1. **添加官方示例音色**：一键把官方示例音频登记为预设音色（无需授权确认）。
   **Fun-CosyVoice3-0.5B 模型本身不带预设音色（无 spk2info.pt）**，因此
   "模型预设音色"（inference_sft）对该模型不可用——首次使用请先添加本示例
   音色或创建个人音色再设为默认。
2. **创建个人音色**：填音色名称 + 勾选「我是该录音的说话人/已获得授权」+
   选择录音文件（wav/mp3/flac/ogg/m4a，≤10MB，建议 5~15 秒安静环境朗读）+
   填**参考文本**（录音中实际说出的每一个字，必须逐字准确）→ 创建。
3. **试听**：点某音色的「试听」，用试听文本框里的内容合成播放。
4. **设为默认**：试听满意后设为默认音色；之后管家回复朗读、语音模式播报
   都会使用该音色。
5. **授权状态**：`pending`（未授权）的音色**无法合成**（会报"尚未获得说话人
   授权"）；这是强制门禁，未经授权的录音不能用于克隆。
6. **删除**：删除档案时同步删除其参考录音与生成缓存，不可恢复。

数据位置（均在 .gitignore 内，不入 Git）：

```
data\voices\profiles.json    音色档案索引
data\voices\audio\           参考录音原件
data\voices\cache\           合成缓存（LRU，同文本同音色重复播报不重跑模型）
```

## 8. 家庭成员声纹识别（阶段 13）

语音模式可以自动识别"现在是谁在说话"，并按成员个性化：称呼、按人隔离的记忆、
其绑定的回答音色。识别不出按"陌生人"回落通用模式。

### 原理与部署

- 声纹提取复用 CosyVoice 服务端已加载的 campplus 模型：适配层新增
  `POST /speaker_embed`（音频 → 192 维向量），主项目不做任何音频算法；
- 主项目 `butler/voice/speakers.py`：SpeakerRegistry（注册/比对/阈值/识别记录）
  + MemberContext（当前说话人占位）；数据在 `data/speakers/speakers.json`
  （.gitignore 覆盖）；
- **注册用的原始录音提取向量后即丢弃**（最小化生物特征留存），API 永不返回向量。

### 启用步骤

1. `.env`：`SPEAKER_RECOGNITION=1`（默认 0 关闭，行为不变）；
   `SPEAKER_THRESHOLD=0.30`（相似度阈值，误判陌生人就调低、放错人就调高）；
2. 启动 CosyVoice 服务（声纹提取依赖它在线）；
3. Web 界面 →「家庭成员声纹」卡片：填称呼 + 选择该成员 5~15 秒清晰朗读录音
   + 勾选成员本人/监护人授权 + 可绑定回答音色 → 注册成员；
4. `python -m butler.voice --mic` 对管家说话：每轮录音先声纹识别再回答，
   终端会打印 `[声纹] 已识别：爸爸（相似度 0.87）`；
5. 键盘输入模式可用 `/who 名字` 手动切换身份（调试用），`/who off` 恢复。

### 隔离语义（阶段 13 核心）

| 谁在说话 | 系统提示词里的记忆 | 保存的新记忆归属 |
|---|---|---|
| 已识别成员 | TA 的个人记忆 + 共享记忆 | 该成员个人 |
| 未识别（陌生人/文本模式） | **仅共享记忆**（不能翻任何人的个人记忆） | 家庭共享 |
| Web 记忆管理 / CLI /memory | 全部（管理视图） | — |

### 边界（务必知悉）

- **声纹不是身份验证**：放一段家人的录音即可冒充。设备控制的 y/N 确认
  流程不因识别结果放松；声纹只用于称呼/记忆/音色个性化；
- 本机实测：同说话人相似度 1.000，不同说话人 0.099（阈值 0.30 分离度充足）；
  小孩变声/远场/噪声会降低相似度，识别不出会自动回落，不会硬猜；
- 删除成员即删除其声纹向量，不可恢复。

## 9. 切回 SAPI

.env 改回 `TTS_BACKEND=sapi`（或直接删掉 TTS_BACKEND 行）→ 重启管家。
音色库数据保留，不影响下次再启用。

## 10. 排错

| 现象 | 原因与处理 |
|---|---|
| Web 界面显示「CosyVoice 服务离线」 | 服务没启动/端口不对：跑 `scripts\cosyvoice_health.bat`，200 才是就绪；模型加载期间会一直不通 |
| 报「无法连接 CosyVoice 服务」 | 同上；确认 .env 的 COSYVOICE_URL 与启动端口一致 |
| 合成超时 | 文本太长或 CPU 推理慢：调大 COSYVOICE_TIMEOUT_SECONDS、缩短文本 |
| `CUDA out of memory` | 显存不足：关掉占显存的程序；仍不行则用 CPU（启动器加 `--model_dir` 不变，设环境变量 `CUDA_VISIBLE_DEVICES=` 空）或换更小模型 |
| 报「尚未获得说话人授权」 | 音色库中该音色是 pending 状态，点「确认授权」或换音色 |
| 报「不支持的音频格式 / 文件头校验失败」 | 上传的文件扩展名与真实格式不符；用原始录音文件 |
| 中文数字/英文发音怪 | 模型特性：CosyVoice3 支持发音修正（如 `报[j][ǐ]`），参考官方 README hotfix 用法 |
| 报 `<|endofprompt|> not detected` | .env 把 COSYVOICE_PROMPT_PREFIX 清空了；CosyVoice3 必须保留官方前缀 |
| 报 `wav sample rate ... must be greater than 16000` | 参考录音采样率低于 16kHz，换用 ≥16kHz 的录音（手机录音一般为 44.1/48kHz） |
| 报 `Calculated padded input size ... Kernel size` | 参考录音太短（不足 1 秒级），换 5~15 秒录音 |
| 本机请求返回 502 | 系统代理劫持了 127.0.0.1 请求；主项目客户端已禁用代理（trust_env=False），若用 curl 测试加 --noproxy 127.0.0.1 |
| 声纹注册报「声纹提取失败」 | CosyVoice 服务离线或音频采样率 <16kHz；先跑 health 检查 |
| 家人总被识别成陌生人 | 调低 SPEAKER_THRESHOLD（如 0.25）或用更高质量的录音重新注册 |
| 陌生人被认成家人 | 调高 SPEAKER_THRESHOLD；声纹本身可被录音冒充，勿用于权限 |
| whisper 安装失败（隔离环境） | 本文档 §3 的两步安装法（setuptools<81 + --no-build-isolation） |
| 想彻底重来 | 关服务（stop.bat）→ 删除 `cosyvoice\` 目录 → 重新跑 setup.bat |

## 11. 本机实际执行记录（2026-10-28）

以下操作已在本机真实执行并通过验证（2026-10-28）：

- uv 安装 Python 3.10.22 并创建 `cosyvoice/.venv`；
- torch 2.3.1+cu121 安装，`torch.cuda.is_available() == True`（RTX 4060 Laptop）；
- 官方 requirements 全量安装成功（含 wetext 替代 ttsfrd 的官方 Windows 路径）；
- 官方仓库 clone（含 Matcha-TTS 子模块）；
- 模型下载完成（FunAudioLLM/Fun-CosyVoice3-0.5B-2512，实际占用 9.1 GB，
  其中 llm.rl.pt 约 2 GB 为官方附带的备选权重，加载代码不使用）；
- 服务启动（模型加载约 55 秒）与健康检查 200；
- **真实中文合成成功**：`python scripts/cosyvoice_selftest.py` 零样本克隆
  合成"你好，卧室现在的温度是二十五度。"→ 3.0 秒音频 / 3.2 秒耗时
  （**RTF≈1.08**，含预热首调 4.1s/3.7s），WAV 规格 mono/24000Hz/16bit；
- 主项目端到端：创建授权音色档案 → `speak_to_file` 合成管家回复
  （5.0s/253KB）→ 同文本缓存命中 0.000s → Web 界面"试听"按钮
  走通 UI→API→模型→浏览器播放全链路；
- GPU 显存占用约 3.6 GB / 8 GB；CPU 推理未单独压测（RTF 预计显著大于 1）；
- **声纹识别实测**：`POST /speaker_embed` 提取 192 维向量 0.58s；同说话人
  相似度 1.000 命中，不同说话人 0.099 判陌生人（阈值 0.30）；按成员记忆隔离
  经真实 LLM 验证：爸爸能看到自己的偏好、妈妈与陌生人看不到；

以下仅提供命令、未在本机执行：Docker/WSL2 路线（本机 WSL2 未安装，不擅自
安装系统组件）、gRPC 服务端、官方流式推理（第一版策略为非流式优先）。
**音质与音色相似度需人工试听确认**（程序只能验证音频规格与非空）。
