@echo off
chcp 65001 >nul
REM ============================================================
REM 下载 Fun-CosyVoice3-0.5B-2512 模型（约 4.9 GB，Apache-2.0）
REM 存放位置：cosyvoice\pretrained_models\Fun-CosyVoice3-0.5B（不入 Git）
REM ============================================================
cd /d "%~dp0.."

if not exist cosyvoice\.venv\Scripts\modelscope.exe (
  echo [错误] 未找到隔离环境，请先运行 scripts\cosyvoice_setup.bat
  pause & exit /b 2
)

if exist cosyvoice\pretrained_models\Fun-CosyVoice3-0.5B\cosyvoice3.yaml (
  echo 模型已存在（cosyvoice3.yaml 找到），如需重下请先删除该目录。
  pause & exit /b 0
)

cosyvoice\.venv\Scripts\modelscope.exe download --model FunAudioLLM/Fun-CosyVoice3-0.5B-2512 --local_dir cosyvoice\pretrained_models\Fun-CosyVoice3-0.5B

echo.
echo 下载完成。下一步：运行 scripts\cosyvoice_start.bat 启动服务
pause
