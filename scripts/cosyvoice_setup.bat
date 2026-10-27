@echo off
chcp 65001 >nul
REM ============================================================
REM CosyVoice 隔离环境搭建（一次性；在本仓库根目录运行）
REM 产物：cosyvoice\.venv（Python 3.10 独立环境，不污染主项目 Python 3.9）
REM 前置：uv 已安装（uv --version 验证）
REM ============================================================
cd /d "%~dp0.."

if not exist cosyvoice\CosyVoice\README.md (
  echo [1/5] 克隆官方仓库（含 Matcha-TTS 子模块）...
  mkdir cosyvoice 2>nul
  git clone --recursive https://github.com/QwenAudio/CosyVoice.git cosyvoice\CosyVoice
) else (
  echo [1/5] 官方仓库已存在，跳过
)

if not exist cosyvoice\.venv\Scripts\python.exe (
  echo [2/5] 创建 Python 3.10 隔离环境...
  uv venv cosyvoice\.venv --python 3.10
) else (
  echo [2/5] 隔离环境已存在，跳过
)

echo [3/5] 安装 PyTorch 2.3.1 + CUDA 12.1（约 2.5 GB，一次性下载）...
uv pip install --python cosyvoice\.venv torch==2.3.1 torchaudio==2.3.1 --index-url https://download.pytorch.org/whl/cu121

echo [4/5] 安装官方 requirements（multi-index；openai-whisper 因构建隔离缺 pkg_resources 需单独装）...
findstr /v "openai-whisper" cosyvoice\CosyVoice\requirements.txt > cosyvoice\requirements-without-whisper.txt
uv pip install --python cosyvoice\.venv --index-strategy unsafe-best-match -r cosyvoice\requirements-without-whisper.txt

echo [5/5] 安装 openai-whisper（sdist，需在环境内预置 setuptools 后关闭构建隔离）...
uv pip install --python cosyvoice\.venv setuptools wheel
uv pip install --python cosyvoice\.venv --no-build-isolation openai-whisper==20231117

echo.
echo 完成。下一步：运行 scripts\cosyvoice_download_model.bat 下载模型（约 4.9 GB）
pause
