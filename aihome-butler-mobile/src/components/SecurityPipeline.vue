<template>
  <view class="sp-panel glass-card">
    <view class="sp-head">
      <text class="sp-title">🛡️ 安全流水线（程序层强制）</text>
      <text v-if="chat.pipelineTraceId" class="sp-trace mono">trace {{ chat.pipelineTraceId }}</text>
    </view>
    <view
      v-for="(node, idx) in chat.pipelineNodes"
      :key="node.step"
      class="pipe-step"
      :class="statusClass(node.status)"
    >
      <view class="pipe-num mono">{{ idx + 1 }}</view>
      <view class="pipe-body">
        <text class="pipe-name">{{ node.label }}</text>
        <text class="pipe-step-id mono">{{ node.step }}</text>
      </view>
      <text v-if="node.detail" class="pipe-detail">{{ node.detail }}</text>
      <text v-else class="pipe-detail pipe-detail-empty">{{ statusText(node.status) }}</text>
    </view>
    <view v-if="!chat.pipelineHasEvents" class="sp-empty">
      <text>暂无执行记录：发起设备控制或对话后，8 步安全流水线将在此实时呈现</text>
    </view>
  </view>
</template>

<script setup lang="ts">
import { useChatStore, type PipelineNodeStatus } from '@/stores/chat';

const chat = useChatStore();

/** 节点状态着色：ok=绿、pending=蓝脉冲、warn=琥珀、error=红、skipped=灰（Web .pipe-step 同源）。 */
function statusClass(status: PipelineNodeStatus): string {
  return `ps-${status}`;
}

function statusText(status: PipelineNodeStatus): string {
  const map: Record<PipelineNodeStatus, string> = {
    ok: '通过',
    warn: '告警',
    error: '拦截',
    pending: '等待中',
    skipped: '跳过',
    idle: '待触发',
  };
  return map[status];
}
</script>

<style lang="scss" scoped>
.sp-panel {
  padding: 12px;
  margin: 8px 12px;
}
.sp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
  gap: 8px;
}
.sp-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--text-primary);
}
.sp-trace {
  font-size: 11px;
  color: var(--text-secondary);
}
/* 编号行式流水线（Web .pipe-step 同源） */
.pipe-step {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: 11px;
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  margin-bottom: 6px;
}
.pipe-step:last-of-type {
  margin-bottom: 0;
}
.pipe-num {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: var(--bg-page);
  border: 1px solid var(--border-strong);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 700;
  color: var(--text-secondary);
  flex-shrink: 0;
}
.pipe-body {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.pipe-name {
  font-weight: 600;
  font-size: 13px;
  color: var(--text-primary);
}
.pipe-step-id {
  font-size: 10px;
  color: var(--text-secondary);
}
.pipe-detail {
  margin-left: auto;
  text-align: right;
  max-width: 46%;
  font-size: 11px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pipe-detail-empty {
  opacity: 0.6;
}
/* 状态着色：边框 + 底色（与 Web 完全一致） */
.ps-ok {
  border-color: rgba(52, 211, 153, 0.45);
  background: var(--status-ok-bg);
}
.ps-ok .pipe-num {
  border-color: var(--status-ok);
  color: var(--status-ok);
}
.ps-warn {
  border-color: rgba(251, 191, 36, 0.45);
  background: var(--status-warn-bg);
}
.ps-warn .pipe-num {
  border-color: var(--status-warn);
  color: var(--status-warn);
}
.ps-error {
  border-color: rgba(248, 113, 113, 0.5);
  background: var(--status-error-bg);
}
.ps-error .pipe-num {
  border-color: var(--status-error);
  color: var(--status-error);
}
.ps-pending {
  border-color: rgba(56, 189, 248, 0.55);
  animation: pipe-pulse 1.2s infinite;
}
.ps-pending .pipe-num {
  border-color: var(--brand-primary);
  color: var(--brand-primary);
}
.ps-skipped {
  opacity: 0.72;
}
@keyframes pipe-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(56, 189, 248, 0.25); }
  50% { box-shadow: 0 0 0 5px rgba(56, 189, 248, 0.05); }
}
.sp-empty {
  padding: 10px 4px 4px;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.6;
}
</style>
