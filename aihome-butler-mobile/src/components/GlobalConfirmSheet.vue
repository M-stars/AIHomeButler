<template>
  <view v-if="store.count > 0" class="gcs-mask">
    <view class="gcs-sheet">
      <view class="gcs-header">
        <text class="gcs-title">⚠️ 待批准的安全操作</text>
        <text class="gcs-subtitle">请看清摘要与完整参数后手动点选，管家绝不静默执行</text>
      </view>
      <scroll-view scroll-y class="gcs-list">
        <view v-for="item in store.items" :key="item.id" class="gcs-card">
          <view class="gcs-summary">{{ item.summary }}</view>
          <view class="gcs-args-box">
            <text class="gcs-args">{{ formatArgs(item.arguments) }}</text>
          </view>
          <view class="gcs-meta">
            <text>{{ formatTime(item.created_at) }}</text>
          </view>
          <view class="gcs-actions">
            <button
              class="gcs-btn gcs-approve"
              :class="{ 'gcs-btn-disabled': store.isResponding(item.id) }"
              :disabled="store.isResponding(item.id)"
              @click="onApprove(item)"
            >
              ✓ 批准执行
            </button>
            <button
              class="gcs-btn gcs-deny"
              :class="{ 'gcs-btn-disabled': store.isResponding(item.id) }"
              :disabled="store.isResponding(item.id)"
              @click="onDeny(item)"
            >
              ✕ 拒绝
            </button>
          </view>
        </view>
      </scroll-view>
      <view class="gcs-safe-area" />
    </view>
  </view>
</template>

<script setup lang="ts">
import { useConfirmationStore } from '@/stores/confirmation';
import type { ConfirmationItem } from '@/types/api';

const store = useConfirmationStore();

/** 参数明文区：等宽字体展示格式化后的完整参数（审批决策依据，禁止省略）。 */
function formatArgs(args: Record<string, unknown>): string {
  try {
    return JSON.stringify(args ?? {}, null, 2);
  } catch {
    return String(args);
  }
}

function formatTime(iso: string): string {
  return (iso || '').replace('T', ' ').slice(0, 19);
}

function toast(title: string): void {
  (globalThis as any).uni?.showToast?.({ title, icon: 'none' });
}

async function onApprove(item: ConfirmationItem): Promise<void> {
  const ok = await store.respond(item.id, true);
  toast(ok ? '已批准，管家将执行该操作' : '该操作已被处理或提交失败');
}

async function onDeny(item: ConfirmationItem): Promise<void> {
  const ok = await store.respond(item.id, false);
  toast(ok ? '已拒绝，设备不会被改动' : '该操作已被处理或提交失败');
}
</script>

<style lang="scss" scoped>
.gcs-mask {
  position: fixed;
  left: 0;
  right: 0;
  top: 0;
  bottom: 0;
  background: rgba(2, 6, 23, 0.55);
  z-index: 1000;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
}
/* 琥珀渐变确认面板（Web .confirm-banner 同源） */
.gcs-sheet {
  background: var(--bg-card);
  backdrop-filter: blur(16px);
  border-top-left-radius: 20px;
  border-top-right-radius: 20px;
  border-top: 1px solid rgba(251, 191, 36, 0.45);
  padding: 18px 16px 0 16px;
  max-height: 72vh;
  display: flex;
  flex-direction: column;
}
.gcs-header {
  margin-bottom: 12px;
  padding: 12px 14px;
  border-radius: var(--radius-card);
  background: linear-gradient(135deg, rgba(251, 191, 36, 0.16), rgba(245, 158, 11, 0.08));
  border: 1px solid rgba(251, 191, 36, 0.45);
}
.gcs-title {
  display: block;
  font-size: 17px;
  font-weight: 700;
  color: var(--status-warn);
}
.gcs-subtitle {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.5;
}
.gcs-list {
  flex: 1;
  max-height: 56vh;
}
.gcs-card {
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-card);
  padding: 12px;
  margin-bottom: 10px;
}
.gcs-summary {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
  line-height: 1.4;
}
.gcs-args-box {
  margin-top: 8px;
  background: rgba(2, 6, 23, 0.45);
  border: 1px dashed rgba(56, 189, 248, 0.3);
  border-radius: var(--radius-control);
  padding: 8px 10px;
  overflow: hidden;
}
.gcs-args {
  font-family: ui-monospace, Consolas, Menlo, monospace;
  font-size: 12px;
  line-height: 1.5;
  color: var(--brand-primary);
  white-space: pre-wrap;
  word-break: break-all;
}
.gcs-meta {
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-secondary);
}
.gcs-actions {
  margin-top: 10px;
  display: flex;
  gap: 10px;
}
.gcs-btn {
  flex: 1;
  min-height: 44px;
  line-height: 44px;
  border-radius: var(--radius-control);
  font-size: 15px;
  font-weight: 600;
  padding: 0;
  margin: 0;
}
.gcs-btn::after {
  border: none;
}
.gcs-approve {
  background: #10b981;
  color: #ffffff;
}
.gcs-deny {
  background: #ef4444;
  color: #ffffff;
}
.gcs-btn-disabled {
  opacity: 0.45;
}
/* 全面屏底部安全区适配（§6） */
.gcs-safe-area {
  height: 12px;
  padding-bottom: constant(safe-area-inset-bottom);
  padding-bottom: env(safe-area-inset-bottom);
}
</style>
