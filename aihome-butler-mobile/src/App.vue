<script setup lang="ts">
import { onLaunch, onShow, onHide } from '@dcloudio/uni-app';
import { useAuthStore } from '@/stores/auth';
import { startRealtime, setRealtimeForeground } from '@/services/realtimeBinding';

const auth = useAuthStore();

onLaunch(async () => {
  // 主题先于一切应用，避免首帧闪白
  auth.applyNavigationBarTheme();
  await auth.bootstrap();
  if (auth.needsPairing || !auth.token) {
    // 未配对：直接进入配对引导页（验收清单第 1 项的另一半：重启免配对）
    (globalThis as any).uni?.reLaunch?.({ url: '/pages/pairing/index' });
    return;
  }
  startRealtime();
});

onShow(() => {
  // App 切回前台：实时层立刻重连并拉一次 overview 与 confirmations
  setRealtimeForeground(true);
});

onHide(() => {
  // App 切后台：主动断开 SSE/轮询省电
  setRealtimeForeground(false);
});
</script>

<style>
/* ========== 设计 Token（对齐 Web 控制台 butler/web/static/styles.css）========== */
/* Dark Theme（默认）：暗黑玻璃态 + 双径向光晕背景 */
.theme-dark {
  --bg-page: #090e1a;
  --bg-grad: radial-gradient(circle at 12% 10%, rgba(56, 189, 248, 0.14), transparent 42%),
    radial-gradient(circle at 88% 82%, rgba(99, 102, 241, 0.14), transparent 45%);
  --bg-card: rgba(17, 24, 39, 0.78);
  --bg-input: rgba(10, 15, 28, 0.85);
  --border-subtle: rgba(148, 163, 184, 0.16);
  --border-strong: rgba(148, 163, 184, 0.28);
  --text-primary: #f1f5f9;
  --text-secondary: #94a3b8;
  --brand-primary: #38bdf8;
  --brand-gradient: linear-gradient(135deg, #38bdf8, #6366f1);
  --status-ok: #34d399;
  --status-ok-bg: rgba(52, 211, 153, 0.14);
  --status-warn: #fbbf24;
  --status-warn-bg: rgba(251, 191, 36, 0.15);
  --status-error: #f87171;
  --status-error-bg: rgba(248, 113, 113, 0.15);
  --shadow-card: 0 12px 32px rgba(0, 0, 0, 0.35);
  --radius-card: 16px;
  --radius-control: 10px;
}
/* Light Theme：一键切换明亮模式（对齐 Web [data-theme=light]） */
.theme-light {
  --bg-page: #f1f5f9;
  --bg-grad: radial-gradient(circle at 10% 10%, rgba(14, 165, 233, 0.12), transparent 40%),
    radial-gradient(circle at 90% 85%, rgba(99, 102, 241, 0.1), transparent 45%);
  --bg-card: rgba(255, 255, 255, 0.88);
  --bg-input: #f8fafc;
  --border-subtle: rgba(15, 23, 42, 0.1);
  --border-strong: rgba(15, 23, 42, 0.2);
  --text-primary: #0f172a;
  --text-secondary: #64748b;
  --brand-primary: #0284c7;
  --brand-gradient: linear-gradient(135deg, #38bdf8, #6366f1);
  --status-ok: #059669;
  --status-ok-bg: rgba(5, 150, 105, 0.12);
  --status-warn: #d97706;
  --status-warn-bg: rgba(217, 119, 6, 0.12);
  --status-error: #dc2626;
  --status-error-bg: rgba(220, 38, 38, 0.1);
  --shadow-card: 0 8px 24px rgba(15, 23, 42, 0.08);
  --radius-card: 16px;
  --radius-control: 10px;
}

page {
  background-color: #090e1a;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
}

/* ========== 通用元素（与 Web 控制台同视觉语言）========== */
.page-root {
  min-height: 100vh;
  padding-bottom: 20px;
  background: var(--bg-page);
  background-image: var(--bg-grad);
  color: var(--text-primary);
  box-sizing: border-box;
}
.section-title {
  display: block;
  margin: 14px 12px 8px;
  font-size: 15px;
  font-weight: 700;
  color: var(--text-primary);
}
.empty-hint {
  margin: 20px 12px;
  padding: 18px 14px;
  background: var(--bg-card);
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-card);
  color: var(--text-secondary);
  font-size: 13px;
  text-align: center;
  line-height: 1.6;
}
/* 玻璃态卡片（Web .card 同源） */
.glass-card {
  background: var(--bg-card);
  backdrop-filter: blur(14px);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-card);
}
.primary-btn {
  background: var(--brand-gradient);
  color: #ffffff;
  border: none;
  border-radius: var(--radius-control);
  min-height: 44px;
  line-height: 44px;
  font-size: 15px;
  font-weight: 600;
  padding: 0 16px;
  margin: 0;
}
.primary-btn::after {
  border: none;
}
.primary-btn:active {
  transform: scale(0.98);
}
.primary-btn[disabled] {
  opacity: 0.45;
}
.ghost-btn {
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  color: var(--text-primary);
  border-radius: var(--radius-control);
  min-height: 44px;
  line-height: 42px;
  font-size: 14px;
  padding: 0 16px;
  margin: 0;
}
.ghost-btn::after {
  border: none;
}
.danger-btn {
  background: #ef4444;
  color: #ffffff;
  border: none;
  border-radius: var(--radius-control);
  min-height: 44px;
  line-height: 44px;
  font-size: 15px;
  font-weight: 600;
  padding: 0 16px;
  margin: 0;
}
.danger-btn::after {
  border: none;
}
.text-input {
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-control);
  color: var(--text-primary);
  font-size: 14px;
  padding: 10px 12px;
  min-height: 44px;
  box-sizing: border-box;
}
.mono {
  font-family: ui-monospace, Consolas, Menlo, monospace;
}
</style>
