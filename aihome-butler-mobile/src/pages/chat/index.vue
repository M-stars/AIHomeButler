<template>
  <view class="page-root" :class="auth.theme === 'light' ? 'theme-light' : 'theme-dark'">
    <!-- 顶部模式切换 + 流水线入口 -->
    <view class="chat-top">
      <view v-if="plannerEnabled" class="mode-seg">
        <view
          class="mode-item"
          :class="{ 'mode-item-active': chat.mode === 'chat' }"
          @click="chat.setMode('chat')"
        >
          <text>自然语言对话</text>
        </view>
        <view
          class="mode-item"
          :class="{ 'mode-item-active': chat.mode === 'task' }"
          @click="chat.setMode('task')"
        >
          <text>多步骤任务规划</text>
        </view>
      </view>
      <view v-else class="mode-single">
        <text>自然语言对话</text>
      </view>
      <view class="pipeline-entry" @click="chat.togglePipelinePanel()">
        <text>🛡️ 安全流水线</text>
        <view v-if="chat.pipelineHot" class="hot-dot" />
      </view>
    </view>

    <!-- 实时 8 步安全流水线面板 -->
    <security-pipeline v-if="chat.pipelinePanelVisible" />

    <!-- 消息流 -->
    <scroll-view scroll-y class="msg-scroll" :scroll-into-view="lastMsgAnchor">
      <view class="msg-list">
        <view v-for="msg in chat.messages" :key="msg.id" :id="msg.id">
          <!-- 用户消息靠右 -->
          <view v-if="msg.role === 'user'" class="bubble-row row-user">
            <view class="bubble bubble-user">
              <text>{{ msg.text }}</text>
            </view>
          </view>

          <!-- 管家消息靠左：工具摘要标签 + 气泡 + 语音播报 -->
          <view v-else class="bubble-row row-butler">
            <view v-if="msg.toolEvents.length > 0" class="tool-chips">
              <view v-for="(ev, i) in msg.toolEvents" :key="i" class="tool-chip" @click="toggleToolDetail(msg.id, i)">
                <text>🔧 {{ ev.tool }} · {{ ev.status }}</text>
              </view>
            </view>
            <view v-for="(ev, i) in msg.toolEvents" :key="'d' + i">
              <view v-if="openToolDetail === `${msg.id}:${i}` && ev.message" class="tool-detail">
                <text>{{ ev.message }}</text>
              </view>
            </view>
            <view v-if="msg.kind === 'task-report' && msg.taskReport" class="bubble bubble-butler task-card">
              <view class="task-head">
                <text class="task-status" :class="taskStatusClass(msg.taskReport.status)">
                  {{ taskStatusLabel(msg.taskReport.status) }}
                </text>
              </view>
              <view v-for="(step, i) in msg.taskReport.steps" :key="i" class="task-step">
                <text class="task-step-dot" :class="taskStatusClass(step.status)" />
                <text class="task-step-text">{{ step.tool }}{{ step.message ? '：' + step.message : '' }}</text>
              </view>
              <text v-if="msg.taskReport.summary" class="task-summary">{{ msg.taskReport.summary }}</text>
            </view>
            <view v-else-if="msg.kind === 'text'" class="bubble bubble-butler bubble-with-tts">
              <text>{{ msg.text }}</text>
              <view v-if="voiceEnabled" class="tts-btn" @click="playTts(msg)">
                <text>{{ ttsLoadingId === msg.id ? '…' : '🔊' }}</text>
              </view>
            </view>
          </view>
        </view>
        <view v-if="chat.messages.length === 0" class="empty-hint">
          <text>试试下方快捷指令，或直接输入"打开客厅的灯"。控制类操作会先弹出安全审批抽屉。</text>
        </view>
        <view :id="'anchor-bottom'" class="anchor-bottom" />
      </view>
    </scroll-view>

    <!-- 底部输入区 -->
    <view class="input-bar">
      <scroll-view scroll-x class="chips-scroll" show-scrollbar="false">
        <view class="chips-row">
          <view v-for="chip in QUICK_CHIPS" :key="chip" class="quick-chip" @click="send(chip)">
            <text>{{ chip }}</text>
          </view>
        </view>
      </scroll-view>
      <view class="input-row">
        <input
          v-model="draft"
          class="text-input input-box"
          confirm-type="send"
          placeholder="给管家发消息…"
          placeholder-class="input-placeholder"
          :disabled="chat.sending"
          @confirm="send(draft)"
        />
        <button class="send-btn" :disabled="chat.sending || !draft.trim()" @click="send(draft)">
          <text>{{ chat.sending ? '…' : '发送' }}</text>
        </button>
      </view>
      <view class="input-safe-area" />
    </view>

    <global-confirm-sheet />
  </view>
</template>

<script setup lang="ts">
import { computed, ref, nextTick } from 'vue';
import { onShow } from '@dcloudio/uni-app';
import { useAuthStore } from '@/stores/auth';
import { useChatStore } from '@/stores/chat';
import { useConfirmationStore } from '@/stores/confirmation';
import { withActiveRealtime } from '@/services/realtimeBinding';
import { playWavBytes } from '@/services/environment';
import { synthesizeTts } from '@/services/butlerApi';
import SecurityPipeline from '@/components/SecurityPipeline.vue';
import GlobalConfirmSheet from '@/components/GlobalConfirmSheet.vue';
import type { ChatMessage } from '@/stores/chat';

const auth = useAuthStore();
const chat = useChatStore();
const confirmations = useConfirmationStore();

const draft = ref('');
const openToolDetail = ref('');
const ttsLoadingId = ref('');
const lastMsgAnchor = ref('');

const QUICK_CHIPS = ['查室温和设备状态', '睡眠场景', '查看长期记忆'];

const plannerEnabled = computed(() => auth.capabilities?.planner === true);
const voiceEnabled = computed(() => auth.capabilities?.voice === true);

onShow(() => {
  confirmations.load();
});

function toast(title: string): void {
  (globalThis as any).uni?.showToast?.({ title, icon: 'none', duration: 2500 });
}

async function send(text: string): Promise<void> {
  const trimmed = (text || '').trim();
  if (!trimmed || chat.sending) return;
  if (auth.channel === 'offline') {
    toast('管家服务未连接，请检查网络后重试');
    return;
  }
  draft.value = '';
  const promise = chat.mode === 'task' ? chat.sendTask(trimmed) : chat.sendChat(trimmed);
  // 对话/任务请求进行期间：轮询模式节奏提到 1s（确认审批及时弹出）
  const result = await withActiveRealtime(promise);
  if (result.conflict) {
    toast('管家正在处理上一条请求，请稍候');
  } else if (result.failed) {
    toast('请求失败，请检查管家服务是否可达');
  }
  scrollBottom();
}

function scrollBottom(): void {
  nextTick(() => {
    lastMsgAnchor.value = 'anchor-bottom';
  });
}

function toggleToolDetail(msgId: string, idx: number): void {
  const key = `${msgId}:${idx}`;
  openToolDetail.value = openToolDetail.value === key ? '' : key;
}

function taskStatusLabel(status: string): string {
  const map: Record<string, string> = {
    completed: '✅ 全部完成',
    partial: '⚠️ 部分完成',
    failed: '❌ 已中止',
  };
  return map[status] || status;
}

function taskStatusClass(status: string): string {
  const map: Record<string, string> = {
    completed: 'ts-ok',
    partial: 'ts-warn',
    failed: 'ts-error',
  };
  return map[status] || 'ts-idle';
}

/** 语音播报关家回复（仅 capabilities.voice 时显示）。 */
async function playTts(msg: ChatMessage): Promise<void> {
  if (ttsLoadingId.value) return;
  ttsLoadingId.value = msg.id;
  try {
    const wav = await synthesizeTts(msg.text.slice(0, 300));
    await playWavBytes(wav);
  } catch (exc) {
    const detail = (exc as { detail?: string }).detail;
    toast(detail || '语音合成失败（TTS 服务可能未就绪）');
  } finally {
    ttsLoadingId.value = '';
  }
}
</script>

<style lang="scss" scoped>
.chat-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px 4px;
  gap: 8px;
}
.mode-seg {
  display: flex;
  background: rgba(148, 163, 184, 0.14);
  border-radius: var(--radius-control);
  overflow: hidden;
  flex: 1;
}
.mode-item {
  flex: 1;
  text-align: center;
  font-size: 12px;
  color: var(--text-secondary);
  padding: 8px 4px;
}
.mode-item-active {
  background: var(--brand-gradient);
  color: #ffffff;
  font-weight: 600;
}
.mode-single {
  flex: 1;
  font-size: 13px;
  font-weight: 700;
  color: var(--text-primary);
  padding: 8px 4px;
}
.pipeline-entry {
  position: relative;
  font-size: 12px;
  color: var(--brand-primary);
  background: rgba(56, 189, 248, 0.12);
  border: 1px solid rgba(56, 189, 248, 0.35);
  border-radius: 999px;
  padding: 6px 12px;
  flex-shrink: 0;
}
.hot-dot {
  position: absolute;
  top: -3px;
  right: -3px;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--status-warn);
  animation: hot-breathe 1.6s ease-in-out infinite;
}
@keyframes hot-breathe {
  0%, 100% { opacity: 0.4; transform: scale(0.85); }
  50% { opacity: 1; transform: scale(1.15); }
}
.msg-scroll {
  height: calc(100vh - 210px);
}
.msg-list {
  padding: 10px 12px 20px;
}
.anchor-bottom {
  height: 4px;
}
.bubble-row {
  display: flex;
  flex-direction: column;
  margin-bottom: 14px;
}
.row-user {
  align-items: flex-end;
}
.row-butler {
  align-items: flex-start;
}
.bubble {
  max-width: 86%;
  border-radius: 14px;
  padding: 10px 14px;
  font-size: 14px;
  line-height: 1.55;
  word-break: break-all;
  white-space: pre-wrap;
}
/* 不对称圆角（Web .bubble.user/.butler 同源） */
.bubble-user {
  background: var(--brand-gradient);
  color: #ffffff;
  border-bottom-right-radius: 4px;
}
.bubble-butler {
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  color: var(--text-primary);
  border-bottom-left-radius: 4px;
}
.bubble-with-tts {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.bubble-with-tts text {
  flex: 1;
}
.tts-btn {
  width: 30px;
  height: 30px;
  line-height: 30px;
  text-align: center;
  border-radius: 50%;
  background: rgba(56, 189, 248, 0.14);
  flex-shrink: 0;
}
/* 工具事件虚线标签（Web .bubble.tool 同源） */
.tool-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}
.tool-chip {
  font-size: 12px;
  color: var(--text-secondary);
  background: rgba(56, 189, 248, 0.08);
  border: 1px dashed rgba(56, 189, 248, 0.25);
  border-radius: 999px;
  padding: 4px 11px;
}
.tool-detail {
  font-size: 11px;
  color: var(--text-secondary);
  background: var(--bg-input);
  border-radius: var(--radius-control);
  padding: 6px 10px;
  margin-bottom: 6px;
  max-width: 82%;
}
.task-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.task-head {
  display: flex;
}
.task-status {
  font-size: 12px;
  font-weight: 700;
}
.ts-ok {
  color: var(--status-ok);
}
.ts-warn {
  color: var(--status-warn);
}
.ts-error {
  color: var(--status-error);
}
.ts-idle {
  color: var(--text-secondary);
}
.task-step {
  display: flex;
  align-items: flex-start;
  gap: 6px;
}
.task-step-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-top: 6px;
  flex-shrink: 0;
  background: currentColor;
}
.task-step-text {
  font-size: 13px;
  color: var(--text-primary);
  line-height: 1.5;
}
.task-summary {
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.5;
}
.input-bar {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 50px;
  background: var(--bg-page);
  border-top: 1px solid var(--border-subtle);
}
.chips-scroll {
  white-space: nowrap;
}
.chips-row {
  display: inline-flex;
  gap: 8px;
  padding: 8px 12px 0;
}
.quick-chip {
  font-size: 12px;
  color: var(--brand-primary);
  background: rgba(56, 189, 248, 0.1);
  border: 1px solid rgba(56, 189, 248, 0.3);
  border-radius: 999px;
  padding: 5px 12px;
}
.input-row {
  display: flex;
  gap: 8px;
  padding: 8px 12px;
  align-items: center;
}
.input-box {
  flex: 1;
}
.input-placeholder {
  color: rgba(148, 163, 184, 0.55);
}
.send-btn {
  width: 76px;
  min-height: 40px;
  line-height: 40px;
  font-size: 14px;
  padding: 0;
  margin: 0;
  background: var(--brand-gradient);
  color: #ffffff;
  border-radius: var(--radius-control);
}
.send-btn::after {
  border: none;
}
.send-btn[disabled] {
  opacity: 0.45;
}
.input-safe-area {
  padding-bottom: constant(safe-area-inset-bottom);
  padding-bottom: env(safe-area-inset-bottom);
  height: 4px;
}
</style>
