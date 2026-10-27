@echo off
chcp 65001 >nul
REM ============================================================
REM 检查 CosyVoice 服务健康状态（/docs 探针，与主项目同判据）
REM ============================================================
curl -s -o nul -w "HTTP %%{http_code}" --max-time 5 http://127.0.0.1:9880/docs
echo.
if %errorlevel%==0 echo （200=服务在线；000/连接失败=服务未启动）
pause
