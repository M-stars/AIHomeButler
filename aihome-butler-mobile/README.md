# AI Home Butler — PE 端（移动端）App

> 对接 `AIHomeButler` 服务端 V2.0（FastAPI，协议版本 `1.0`，默认端口 `8080`）。
> 设计文档：[`docs/MOBILE_APP_SPEC.md`](../docs/MOBILE_APP_SPEC.md)（产品定位、页面交互、
> API 契约、实时通信策略、视觉 Token、验收清单 §8.2 均以该文档为准）。
> UI 视觉与 [`butler/web/`](../butler/web/) Web 控制台同源：暗黑玻璃态 + 双径向光晕背景、
> 品牌渐变 `#38bdf8 → #6366f1`、编号式安全流水线、琥珀色确认横幅。

## 定位与边界（务必遵守）

- App = 树莓派管家的**移动遥控器 + AI 对话终端 + 安全审批端 + 配置台**；
- 不在手机本地跑任何模型/ASR/TTS；不直连 GPIO 或 Home Assistant；
- 不绕过服务端高风险设备限制（lock/alarm/siren/water_heater 服务端结构性 403）；
- 绝不提供通知栏一键静默批准——审批必须打开 App 看清摘要与完整参数后点选。

## 技术栈

uni-app（Vue 3 + TypeScript + Pinia + Vite），一套代码产出：

| 目标 | 命令 | 产物 |
|---|---|---|
| H5 / PWA | `npm run build:h5` | `dist/build/h5/` |
| 微信小程序 | `npm run build:mp-weixin` | `dist/build/mp-weixin/`（微信开发者工具导入运行） |
| Android App | `npm run build:app` 后用 HBuilderX 云打包 / 本地 Android SDK 签名 APK | `.apk`（需 HBuilderX 或 Android SDK，本仓库未含签名环境） |

```bash
npm install          # 安装依赖
npm run dev:h5       # 本地开发（浏览器，CORS 已在服务端放开）
npm run test         # Vitest 单元测试（73 项，全离线）
npm run type-check   # vue-tsc 类型检查
```

## 目录速查

```
src/
├── types/api.ts            # 服务端契约类型（对照 webapp.py 逐行核验，禁止自造字段）
├── services/
│   ├── http.ts             # 统一请求：Bearer 注入 / 401 拦截 / 超时分级（15s，对话任务 180s）
│   ├── connection.ts       # LAN(1.5s)/Cloud(3s) 双通道探测切换状态机
│   ├── realtime.ts         # SSE(H5 EventSource/App chunked 流式)/小程序短轮询自适应 + 指数退避
│   ├── butlerApi.ts        # 全量 API 封装
│   ├── sensor.ts           # 传感器跨后端字段兼容（temperature_c/temperature 等优先级）
│   └── realtimeBinding.ts  # realtime ↔ pinia stores 组合根
├── stores/                 # auth / devices / chat / confirmation 四个 pinia store
├── components/             # GlobalConfirmSheet / DeviceCard / SensorStrip / SecurityPipeline
└── pages/                  # pairing / home / chat / automation / settings（底部 4 Tab）
```

## 关键机制（与规格 §5 对齐）

- **防重放**：亮度/位置/湿度滑条只在松手（`@change`）时发请求；开关类控件 400ms 请求中锁；
- **直控回读语义**：`ok+verified` → toast「已执行并验证」；`duplicate_skipped` → 「3 秒内已执行过相同
  操作」；`unknown` → 弹窗黄色告警「指令可能已发送，但回读状态未知」，绝不渲染为成功；
  HTTP 400/403 → 回滚控件并展示服务端 `detail`；
- **确认审批**：SSE `confirmation_pending` 实时弹出全局审批抽屉（轮询兜底），对话请求超时 ≥180s
  （服务端确认队列最长等 120s，超时设短会在用户点批准前断开）；
- **401 收敛**：任一接口 401 → 清本地凭证 → 跳回配对页（依赖服务端 401 携带 CORS 头，
  见 `butler/webapp.py` 鉴权中间件注释与 `tests/test_webapp.py` 回归测试）。

## 端到端冒烟记录（2026-11-21，模拟后端 + H5）

配对闭环（探测→6 位码→`bh_` 令牌→重载免配对）、开关灯直控（verified+审计
`actor=device_app:cli_xxx`）、对话触发确认→抽屉批准→设备动作、8 步安全流水线 SSE 实时渲染、
管理台踢除后 401→清凭证→回配对页，均已实测通过。

## 待办（Milestone 5 尾项）

- Android 签名测试 APK：需 HBuilderX（云打包）或本地 Android Studio/SDK；
- 微信小程序：`dist/build/mp-weixin` 已产出，需在微信开发者工具中导入体验（appid 在
  `src/manifest.json` 的 `mp-weixin.appid` 处填写）；
- 树莓派实机联调（LAN 直连真机）。
