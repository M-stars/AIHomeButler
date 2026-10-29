@echo off
chcp 65001 >nul
REM ============================================================
REM 启动 CosyVoice 官方服务端（仅监听 127.0.0.1，勿暴露局域网）
REM 端口 9880；模型加载约需数十秒，就绪后 GET /docs 返回 200
REM ============================================================
cd /d "%~dp0.."

if not exist cosyvoice\.venv\Scripts\python.exe (
  echo [错误] 未找到隔离环境，请先运行 scripts\cosyvoice_setup.bat
  pause & exit /b 2
)
if not exist cosyvoice\pretrained_models\Fun-CosyVoice3-0.5B\cosyvoice3.yaml (
  echo [错误] 未找到模型权重，请先运行 scripts\cosyvoice_download_model.bat
  pause & exit /b 2
)

echo 启动 CosyVoice 服务（首次加载模型需数十秒）...
cosyvoice\.venv\Scripts\python.exe scripts\cosyvoice_server.py --host 127.0.0.1 --port 9880 --model_dir cosyvoice\pretrained_models\Fun-CosyVoice3-0.5B
pause
