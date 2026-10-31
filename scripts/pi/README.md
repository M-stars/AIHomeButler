# scripts/pi/ —— 树莓派一键部署

执行顺序（前提见 [docs/PI_DEPLOYMENT.md](../../docs/PI_DEPLOYMENT.md)）：

```
bash 01_base.sh    # 系统更新 + Docker + 换源（需重新登录使 docker 组生效）
bash 02_ha.sh      # HA 容器（检测 ~/ha_data.tar.gz 自动走迁移路线）
bash 03_butler.sh  # 管家 venv/依赖/.env/systemd
bash 04_voice.sh   # 可选：whisper + 唤醒词
bash check.sh      # 自检（部署后/排错时随时重跑）
```

脚本均为幂等设计（重复执行安全）；**均未在真机验证**——硬件到位后按
PI_DEPLOYMENT.md §5 清单逐项打勾，异常按 §7 排查并回填文档。
