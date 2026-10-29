@echo off
chcp 65001 >nul
REM ============================================================
REM 停止 CosyVoice 服务（按端口 9880 找 PID 后结束进程）
REM ============================================================
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :9880 ^| findstr LISTENING') do (
  echo 结束进程 PID=%%p
  taskkill /F /PID %%p
)
echo 完成（若上面没有输出，说明服务本就未运行）。
pause
