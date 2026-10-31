#!/usr/bin/env bash
# 04_voice.sh —— 可选：Pi 侧语音（faster-whisper 识别 + 唤醒词 + espeak 兜底播报）
# 播报主体走 PC 的 CosyVoice（.env 的 COSYVOICE_URL）；本脚本只装本地识别与兜底。
# 用法：bash 04_voice.sh   （在 03_butler.sh 之后执行）
set -euo pipefail

PROJECT_DIR="$HOME/AIHomeButler"
VENV="$PROJECT_DIR/.venv"
PIP_I="-i https://pypi.tuna.tsinghua.edu.cn/simple"

echo "== [1/4] 音频系统依赖 ="
sudo apt-get install -y libportaudio2 espeak-ng alsa-utils

echo "== [2/4] 语音依赖（faster-whisper 识别 + 麦克风采集）=="
"$VENV/bin/pip" install faster-whisper sounddevice $PIP_I
# 注意：纯 CPU int8，Pi 4 实测约 1-2 倍实时（PC 约 10 倍+），延迟敏感可换 small→base

echo "== [3/4] 唤醒词（sherpa-onnx 离线 KWS）=="
"$VENV/bin/pip" install sherpa-onnx $PIP_I || echo "警告：sherpa-onnx 安装失败，唤醒词功能降级，其余不受影响"
if [ -x scripts/wakeword_download_model.py ] || [ -f scripts/wakeword_download_model.py ]; then
  "$VENV/bin/python" scripts/wakeword_download_model.py \
    && echo "   唤醒词模型已就绪（data/models/sherpa-kws/）" \
    || echo "警告：模型下载失败，可稍后重跑本步"
fi

echo "== [4/4] 提示 ="
cat <<'NOTE'

== 04_voice.sh 完成（可选组件）==
- .env 开启：WAKEWORD_ENABLED=1、SPEAKER_RECOGNITION=1（声纹向量提取走 PC CosyVoice）
- 交互测试：.venv/bin/python -m butler.voice --mic --wake
- espeak-ng 仅兜底播报；正式播报请确保 .env 的 TTS_BACKEND=cosyvoice 且 PC 服务可达
NOTE
