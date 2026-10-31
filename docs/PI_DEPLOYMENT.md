# 树莓派部署手册——管家 + Home Assistant 一体机

更新日期：2026-11-01 ｜ 状态：**文档与脚本就绪，待硬件到位实测**（含阶段 14.7 家庭生活服务；
跑通后回填实测记录）

目标拓扑（见部署拓扑决策）：**Pi = 管家进程 + HA 容器 + 声纹注册表**；
阿里云 2C2G = 穿透/推送/监控（阶段 17）；CosyVoice 语音服务留 Windows PC（跨机 HTTP 调用）。

## 0. 采购清单（硬件未购，先买这些）

| 件 | 推荐 | 说明 |
|---|---|---|
| 主板 | **Raspberry Pi 4B 4GB**（首选）或 Pi 5 4GB | HA 容器 + 管家 + whisper 同机的最低舒适线；2GB 需砍语音，3B 不建议 |
| 电源 | 官方 USB-C 5V/3A（Pi 5 需 5V/5A） | 劣质电源 = 随机重启， FIRST KILLER |
| SD 卡 | **64GB A2 级** microSD（闪迪/三星） | HA 镜像 3.5GB + 模型 + 数据库；A2 抗随机写 |
| 散热 | 带风扇外壳（Pi 5 必须主动散热） | 长期 7×24 服务，过热降频 = 卡顿元凶 |
| 网线 | 一根（推荐有线直连路由器） | WiFi 也可，但家庭自动化对延迟抖动敏感 |
| 可选 | USB 全向麦克风 + 3.5mm/USB 小音箱 | 语音输入/播报（阶段 8/13/14 功能） |
| 可选 | ESP32 开发板 | 自制传感器节点（docs/HARDWARE.md），接到 Pi 上的 HA |

预算参考：Pi 4B 4GB 套装（板+电源+壳+散热）约 500-600 元，64GB A2 卡约 50 元。

## 1. 烧录 SD 卡（全新卡）

1. PC 装 [Raspberry Pi Imager](https://www.raspberrypi.com/software/)；
2. 选择系统：**Raspberry Pi OS Lite (64-bit)**——Bookworm 无桌面版（管家是服务，桌面纯浪费内存；
   **必须 64 位**，HA 官方容器镜像无 32 位版）；
3. 点"编辑设置"预配置（免接显示器键盘）：
   - 主机名 `aihome-butler`；开启 SSH（密码认证）；
   - 用户名/密码自定（脚本假定 `pi`，改了请同步改）；WiFi 如不用网线在此填；
   - 语言/时区：`Asia/Shanghai`；
4. 烧录 → 插卡 → 网线/WiFi → 上电，等 2 分钟；
5. PC 上验证：`ssh pi@aihome-butler.local`（失败则在路由器后台查 IP，`ssh pi@<IP>`）。

## 2. 一键部署（scripts/pi/，按序执行）

把代码传到 Pi。**别用 `scp -r` 拷整个项目目录**——会把 PC 上的 CosyVoice 模型
（3GB+，Pi 跑不动）、`ha_data/`、`logs/`、`backups/` 一并塞进 SD 卡。用 git 归档
只带跟踪文件（代码+文档，天然排除 .env/data/缓存）：

```bash
# Windows PC (PowerShell)：
cd D:\AIHomeButler
git archive -o ..\butler_code.tar.gz HEAD
scp ..\butler_code.tar.gz pi@aihome-butler.local:~/
scp .env pi@aihome-butler.local:~/

# Pi 上（git archive 的条目不带顶层目录前缀，直接解进目标目录，勿加 --strip-components）：
mkdir -p ~/AIHomeButler
tar -xzf ~/butler_code.tar.gz -C ~/AIHomeButler
mv ~/.env ~/AIHomeButler/.env
```

（备选：Pi 上直接 `git clone https://github.com/M-stars/AIHomeButler.git ~/AIHomeButler`，
再手动 `scp` .env 与数据文件——.env 与 data/ 不在 git 里，见 §2.2。）

然后 SSH 进 Pi 依次执行（每步 3-15 分钟，国内网络已在脚本内换源处理）：

```bash
cd ~/AIHomeButler/scripts/pi
bash 01_base.sh      # 系统更新 + Docker + 基础依赖（含清华/豆瓣 pip 源）
bash 02_ha.sh        # HA 容器（默认迁移模式：先在 PC 打包 ha_data，见下）
bash 03_butler.sh    # 管家 venv + 依赖 + .env + systemd 服务（开机自启/崩溃自拉）
bash 04_voice.sh     # 可选：faster-whisper 语音识别 + 唤醒词模型
```

### 2.1 迁移 HA 数据（推荐：保留你已配好的一切）

Windows 侧 HA 已配好管理员账号、长期令牌、demo 集成（27 台设备）——整体搬过去，
Pi 上的 HA token 与 PC 完全一致，**无需重新 onboarding**：

```powershell
# PC（先停 Windows 侧容器避免数据变动）：
docker stop homeassistant
tar -czf ha_data.tar.gz -C D:\AIHomeButler ha_data
scp ha_data.tar.gz pi@aihome-butler.local:~/
```

`02_ha.sh` 检测到 `~/ha_data.tar.gz` 会自动解压到 `/opt/homeassistant/config` 并沿用原配置
（`HA_TOKEN` 不变，`.env` 原样可用）。没有该包时脚本走全新安装路线（onboarding 后需
重新生成 token 填入 .env）。设备无需手动配置：管家自动发现 HA 内全部实体并推导能力
（高风险设备类型结构性排除；`data/discovery_exclusions.json` 可按实体排除）。

> 迁移完成并验证后，Windows 侧 `docker rm -f homeassistant` 退役（配置包留档即可）。

### 2.2 管家数据迁移（记忆/规则/家庭台账）

这些运行数据不在 git 里，需从 PC 单独传（长期记忆、规则、家庭台账丢了你就要重新喂）：

| 文件 | 内容 |
|---|---|
| `data/memory.db` | 长期记忆、主动规则、例行任务、通知、审计、token 用量 |
| `data/household.db` | 食材/衣物台账（阶段 14.7：入库记录、穿着记账、穿搭反馈） |
| `data/simulated_state.json` | 模拟设备状态（仅 simulated 后端需要） |
| `data/discovery_exclusions.json` | 设备自动发现排除表（有手工排除才存在） |

⚠️ SQLite 库拷贝前**先退出 PC 侧管家进程**（避免 WAL 半写状态拷出损坏库）；
拷到 Pi 后 `check.sh` 第 6 项会验证台账库可读。

```powershell
# PC（管家已退出）：
scp D:\AIHomeButler\data\memory.db D:\AIHomeButler\data\household.db pi@aihome-butler.local:~/AIHomeButler/data/
```

## 3. .env 配置（Pi 侧）

`03_butler.sh` 会核对 .env 是否就位（git 归档/scp 路线已随 §2 传到 Pi；**.env 不在 git 里**，
git clone 路线需 `scp D:\AIHomeButler\.env pi@...:~/AIHomeButler/.env`）。
Pi 上需要核对/修改的字段：

| 字段 | Pi 上的值 | 说明 |
|---|---|---|
| `DEVICE_BACKEND=homeassistant` | 不变 | HA 与管家同机 |
| `HA_URL=http://localhost:8123` | 不变 | 同机直连 |
| `HA_TOKEN` | 迁移路线不变 / 全新路线填新 token | |
| `COSYVOICE_URL` | `http://<PC局域网IP>:9880` | 跨机调 PC 的 CosyVoice（TTS/声纹向量提取） |
| `TTS_BACKEND=cosyvoice` | 按需 | Pi 无 SAPI；不配则播报用 espeak（音质差，仅调试） |
| `SPEAKER_RECOGNITION=1` | 按需 | 声纹开关注册表在 Pi、向量提取走 PC |
| `WEB_TOKEN` | 不变 | 手机/PC 浏览器访问 Pi 界面用 |
| `WEATHER_LAT` / `WEATHER_LON` | 家里坐标（可选，如杭州 30.27/120.15） | 阶段 14.7 穿搭推荐含天气（Open-Meteo 免费免密钥）；留空则推荐不含天气，其余功能不受影响 |
| `WEATHER_CITY` | 可选 | 仅用于展示的城市名 |

**PC 侧需同步做一件事**：CosyVoice 服务端默认只绑 `127.0.0.1`（防局域网裸奔）。Pi 要跨机
调用，PC 启动时改 `cosyvoice_start.bat` 里的 `--host 0.0.0.0`。⚠️ 该服务无鉴权，**只允许
家内网使用，路由器绝不映射 9880 端口到公网**；介意可改用 SSH 隧道（见 §7 排错）。

## 4. systemd 服务（03_butler.sh 自动安装）

`butler.service`：开机自启、崩溃 5 秒后自动拉起、日志进 journald。

```bash
systemctl status butler        # 看状态
journalctl -u butler -f        # 跟日志
sudo systemctl restart butler  # 改 .env 后重启生效
```

浏览器访问：`http://<Pi的IP>:8080/?token=<WEB_TOKEN>`（手机加主屏 = 阶段 16 前的临时用法）。
⚠️ 红线不变：**8080 只服务家内网，绝不端口映射公网**；外网访问走阶段 17（WireGuard/ntfy）。

## 5. 验证清单（DoD，硬件到位后逐项打勾）

- [ ] `bash scripts/pi/check.sh` 全绿（部署自检脚本：服务状态/HA 连通/设备可读/令牌校验/家庭台账可读）
- [ ] Pi 上 `python -m butler` CLI 冒烟：查询客厅温度 → 返回 demo 数据
- [ ] 浏览器（PC/手机）开 Pi 界面 → 对话「打开客厅灯」→ 确认 → HA 实体状态变化
- [ ] （14.7）对话「今天买了西红柿和排骨」→ 确认清单入库 → `check.sh` 第 6 项台账计数增加
- [ ] （14.7）登记衣物 → 说「今天穿的…」记账 → 问「明天穿什么」返回推荐
  （.env 配了天气坐标则含天气；没配则如实说明不含天气——两者都算通过）
- [ ] （14.7）次日留意巡检通知：临期食材「优先吃掉」/该洗衣物提醒（每项每天至多一次）
- [ ] `sudo reboot` 后：butler 与 HA 均自动恢复，无需人工干预
- [ ] （可选语音）麦克风说话 → whisper 识别 → 播报走 PC CosyVoice
- [ ] （可选声纹）注册一名成员 → 说话识别命中（14.7 衣柜归属自动跟随说话人）
- [ ] （ESP32）烧录后实体出现在 Pi 的 HA，自动发现即可控

## 6. 性能预算（Pi 4B 4GB）

| 组件 | 常驻内存 | 备注 |
|---|---|---|
| Raspberry Pi OS Lite | ~350MB | 无桌面 |
| HA 容器 | ~450MB | 官方镜像 |
| 管家（webapp 模式） | ~250MB | 含 Python/FastAPI/记忆库 |
| faster-whisper small（可选） | ~600MB | 识别时波动；int8 量化 |
| sherpa 唤醒词（可选） | ~120MB | 3.3M 模型，Pi4 实测约 50 倍实时 |
| 家庭台账（household.db） | ≈0 | 14.7：空库几 KB；巡检每 60s 读一次 SQLite，天气 HTTP 30 分钟缓存一次 |
| 余量 | ~2GB | 数据库增长 + 突发 |

已知限制（如实）：whisper 在 Pi 上约 1-2 倍实时（PC 上 10 倍+），语音回复延迟会比 PC 高；
CosyVoice 合成永远在 PC（模型 3GB+，Pi 跑不动）；SD 卡是寿命短板——数据库写入靠管家
现有的低频落库设计，长期可换 SSD（USB 启动）。

## 7. 故障排查（国内环境特供）

| 症状 | 处置 |
|---|---|
| `pip install` 超时 | 01_base.sh 已换清华源；仍慢加 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| Docker 拉 ghcr.io 镜像卡死 | 01_base.sh 已配镜像加速；或手动改 image 为 `ghcr.nju.edu.cn/home-assistant/home-assistant:stable` |
| HA 容器起来但 502/无法 onboarding | 等 2-3 分钟首次初始化；`docker logs homeassistant` 看 frontend 编译 |
| `pip install` 报 externally-managed | Bookworm PEP 668 限制——脚本已全部走 venv，别用 sudo pip 裸装 |
| PC 访问不到 Pi 界面 | `systemctl status butler`；确认同网段、8080 未被 .env 改绑 |
| 声纹/TTS 连不上 PC | PC 端 CosyVoice 是否 `--host 0.0.0.0` 启动、Windows 防火墙放行 9880（仅专用网络）；或 SSH 隧道：`ssh -R 9880:127.0.0.1:9880 pi@<Pi>` 后 Pi 用 `COSYVOICE_URL=http://127.0.0.1:9880` |
| 穿搭推荐提示「天气不可用」 | .env 核对 `WEATHER_LAT`/`WEATHER_LON` 都已填；Pi 直连测试 `curl -m 5 "https://api.open-meteo.com/v1/forecast?latitude=30&longitude=120&current=temperature_2m"`；连不上属网络问题——推荐自动降级为不含天气，是设计内行为不是故障 |
| 家庭台账报错/丢失 | `ls ~/AIHomeButler/data/household.db` 是否存在（没跑 §2.2 迁移）；库损坏时从 PC 备份恢复（backup.ps1 的 zip 含该文件） |
| SD 卡只读/系统损坏 | 典型断电损伤：换卡重烧（数据在 git + 备份脚本里，恢复成本低） |
| 温度过高降频 | `vcgencmd measure_temp` 超 80°C 检查风扇/贴片 |
