@echo off
chcp 65001 >nul
REM ============================================================
REM 下载唤醒词 KWS 模型（sherpa-onnx 中文关键词检测，约 33 MB，Apache-2.0）
REM 存放位置：data\models\sherpa-kws（不入 Git）
REM ============================================================
cd /d "%~dp0.."

python scripts\wakeword_download_model.py %*

echo.
pause
