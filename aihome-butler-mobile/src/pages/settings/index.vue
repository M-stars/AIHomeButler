<template>
  <view class="page-root" :class="auth.theme === 'light' ? 'theme-light' : 'theme-dark'">
    <!-- 1. 长期记忆管理 -->
    <text class="section-title">长期记忆</text>
    <view class="mem-add">
      <picker mode="selector" :range="['偏好 preference', '事实 fact']" @change="onMemKindPick">
        <view class="text-input mem-kind-view"><text>{{ memKind === 'preference' ? '偏好' : '事实' }}</text></view>
      </picker>
      <input v-model="memContent" class="text-input mem-input" placeholder="如 我习惯睡前把空调调到 26 度" placeholder-class="ph" />
      <button class="primary-btn mem-add-btn" :disabled="!memContent.trim() || memSaving" @click="addMemory">
        {{ memSaving ? '…' : '添加' }}
      </button>
    </view>
    <view v-for="m in memories" :key="m.id" class="mem-card">
      <view class="mem-head">
        <text class="mem-kind" :class="m.kind === 'preference' ? 'mem-pref' : 'mem-fact'">
          {{ m.kind === 'preference' ? '偏好' : '事实' }}
        </text>
        <text class="mem-source">{{ m.source === 'user' ? '本人告知' : '管家归纳' }}</text>
        <text class="mem-member">{{ m.member_id || '家庭共享' }}</text>
        <text class="mem-del" @click="deleteMemory(m)">删除</text>
      </view>
      <text class="mem-content">{{ m.content }}</text>
      <text class="mem-time">{{ formatTime(m.updated_at || m.created_at) }}</text>
    </view>
    <view v-if="memories.length === 0" class="empty-hint"><text>暂无长期记忆。</text></view>

    <!-- 2. 管家 AI 个性化人设 -->
    <text class="section-title">管家 AI 人设</text>
    <view class="card">
      <view class="preset-row">
        <view
          v-for="p in persona.presets"
          :key="p.id"
          class="preset-chip"
          :class="{ 'preset-chip-active': personaDraft === p.prompt }"
          @click="applyPreset(p)"
        >
          <text>{{ p.name }}</text>
        </view>
      </view>
      <textarea
        v-model="personaDraft"
        class="text-input persona-textarea"
        :maxlength="1000"
        placeholder="自定义人设提示词（≤1000 字），点击保存即热生效"
        placeholder-class="ph"
      />
      <view class="persona-foot">
        <text class="persona-count">{{ personaDraft.length }}/1000</text>
        <button class="primary-btn persona-save" :disabled="personaSaving" @click="savePersona">
          {{ personaSaving ? '保存中…' : '保存人设' }}
        </button>
      </view>
    </view>

    <!-- 3. 安全审计与用量看板 -->
    <text class="section-title">今日 Token 预算</text>
    <view class="card">
      <template v-if="tokenBudget && tokenBudget.enabled">
        <view class="budget-row">
          <text class="budget-text">已用 {{ formatTokens(tokenBudget.used_today) }} / {{ formatTokens(tokenBudget.limit) }}</text>
          <text class="budget-pct">{{ budgetPct }}%</text>
        </view>
        <view class="budget-bar">
          <view class="budget-fill" :class="budgetFillClass" :style="{ width: budgetBarWidth }" />
        </view>
        <text class="budget-remain">今日剩余 {{ formatTokens(tokenBudget.remaining) }}</text>
      </template>
      <text v-else class="muted">未启用每日 token 预算（不限量）。</text>
    </view>

    <text class="section-title">模型档案（脱敏只读）</text>
    <view v-for="p in models" :key="p.name" class="card model-card">
      <view class="model-row">
        <text class="model-name">{{ p.name }}</text>
        <text class="model-value">{{ p.model }}</text>
      </view>
      <text class="model-muted">{{ p.base_url }} · Key {{ p.api_key_masked || '****' }}</text>
    </view>

    <text class="section-title">安全审计</text>
    <view class="card audit-filter">
      <view
        v-for="opt in AUDIT_FILTERS"
        :key="opt.value"
        class="audit-filter-chip"
        :class="{ 'audit-filter-active': auditFilter === opt.value }"
        @click="auditFilter = opt.value"
      >
        <text>{{ opt.label }}</text>
      </view>
    </view>
    <view v-for="a in filteredAudit" :key="a.id" class="card audit-card">
      <view class="audit-head">
        <text class="audit-actor" :class="actorClass(a.actor)">{{ actorLabel(a.actor) }}</text>
        <text class="audit-action">{{ a.action }}</text>
        <text class="audit-status" :class="auditStatusClass(a.status)">{{ a.status }}</text>
      </view>
      <text class="audit-detail">{{ a.detail }}</text>
      <text class="audit-time">{{ formatTime(a.created_at) }}</text>
    </view>
    <view v-if="audit.length === 0" class="empty-hint"><text>暂无审计记录。</text></view>

    <!-- 语音与声纹状态（只读） -->
    <text class="section-title">语音与声纹</text>
    <view class="card">
      <view class="status-row">
        <text class="status-label">语音合成（TTS）</text>
        <text class="status-value" :class="auth.capabilities?.voice ? 'rule-on' : 'rule-off'">
          {{ auth.capabilities?.voice ? `已启用（${auth.systemInfo?.tts_backend || 'sapi'}）` : '未装配' }}
        </text>
      </view>
      <view class="status-row">
        <text class="status-label">成员声纹识别</text>
        <text class="status-value" :class="speakers?.speaker_recognition ? 'rule-on' : 'rule-off'">
          {{ speakers ? (speakers.speaker_recognition ? `已启用（阈值 ${speakers.threshold}）` : '未启用') : '未装配' }}
        </text>
      </view>
      <view v-if="speakers" class="status-row">
        <text class="status-label">已注册成员</text>
        <text class="status-value">{{ speakers.member_count }} 人（服务 {{ speakers.service_healthy ? '在线' : '离线' }}）</text>
      </view>
      <text v-if="speakers" class="muted disclaimer">{{ speakers.ai_disclaimer }}</text>
    </view>

    <!-- 4. 连接与多端管理 -->
    <text class="section-title">连接与多端管理</text>
    <view class="card">
      <view class="status-row">
        <text class="status-label">当前通道</text>
        <text class="status-value">{{ auth.channelLabel }}</text>
      </view>
      <view class="status-row">
        <text class="status-label">局域网地址</text>
        <text class="status-value small">{{ auth.lanUrl || '未设置' }}</text>
      </view>
      <view class="status-row">
        <text class="status-label">本机设备 ID</text>
        <text class="status-value small">{{ auth.deviceId || '--' }}</text>
      </view>
      <text class="form-label">云中转地址</text>
      <view class="cloud-edit-row">
        <input v-model="cloudDraft" class="text-input cloud-input" placeholder="https://butler.example.com" placeholder-class="ph" />
        <button class="ghost-btn cloud-save" @click="saveCloudUrl">保存</button>
      </view>
      <text class="form-label">外观主题</text>
      <view class="theme-row">
        <view class="day-chip" :class="{ 'day-chip-active': auth.theme === 'dark' }" @click="auth.setTheme('dark')">
          <text>🌙 暗黑</text>
        </view>
        <view class="day-chip" :class="{ 'day-chip-active': auth.theme === 'light' }" @click="auth.setTheme('light')">
          <text>☀️ 明亮</text>
        </view>
      </view>
      <button class="ghost-btn logout-btn" @click="confirmLogout">退出并清除本地凭证</button>
    </view>

    <global-confirm-sheet />
  </view>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { onShow } from '@dcloudio/uni-app';
import { useAuthStore } from '@/stores/auth';
import { useConfirmationStore } from '@/stores/confirmation';
import * as butlerApi from '@/services/butlerApi';
import { ApiError } from '@/services/http';
import GlobalConfirmSheet from '@/components/GlobalConfirmSheet.vue';
import type {
  AuditLogItem,
  MemoryItem,
  ModelProfileView,
  PersonaResponse,
  SpeakersStatusResponse,
  TokenBudgetStatus,
} from '@/types/api';

const auth = useAuthStore();
const confirmations = useConfirmationStore();

const memories = ref<MemoryItem[]>([]);
const memKind = ref<'preference' | 'fact'>('preference');
const memContent = ref('');
const memSaving = ref(false);

const persona = ref<PersonaResponse>({ persona: '', presets: [] });
const personaDraft = ref('');
const personaSaving = ref(false);

const tokenBudget = ref<TokenBudgetStatus | null>(null);
const models = ref<ModelProfileView[]>([]);
const audit = ref<AuditLogItem[]>([]);
const auditFilter = ref('all');
const speakers = ref<SpeakersStatusResponse | null>(null);

const cloudDraft = ref('');

const AUDIT_FILTERS = [
  { value: 'all', label: '全部' },
  { value: 'agent', label: 'agent' },
  { value: 'web_direct', label: '直控' },
  { value: 'device_app', label: '本机' },
  { value: 'user', label: 'user' },
] as const;

onShow(() => {
  confirmations.load();
  loadAll();
});

async function loadAll(): Promise<void> {
  cloudDraft.value = auth.cloudUrl;
  const jobs: Array<Promise<void>> = [
    butlerApi.listMemories()
      .then((r) => { memories.value = r.memories; })
      .catch(() => undefined),
    butlerApi.getPersona()
      .then((r) => {
        persona.value = r;
        personaDraft.value = r.persona || '';
      })
      .catch(() => undefined),
    butlerApi.getModelProfiles()
      .then((r) => { models.value = r.profiles; })
      .catch(() => undefined),
    butlerApi.getAudit(100)
      .then((r) => { audit.value = r.audit; })
      .catch(() => undefined),
    // token 预算随 overview 拉取
    butlerApi.getOverview()
      .then((ov) => { tokenBudget.value = ov.token_budget; })
      .catch(() => undefined),
  ];
  if (auth.capabilities?.speakers) {
    jobs.push(
      butlerApi.getSpeakersStatus()
        .then((s) => { speakers.value = s; })
        .catch(() => { speakers.value = null; }),
    );
  }
  await Promise.all(jobs);
}

function toast(title: string): void {
  (globalThis as any).uni?.showToast?.({ title, icon: 'none', duration: 2500 });
}

function formatTime(iso: string): string {
  return (iso || '').replace('T', ' ').slice(0, 16);
}

function formatTokens(n: number): string {
  if (n >= 10000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

// ---------------- 记忆 ----------------

function onMemKindPick(e: { detail: { value: number | string } }): void {
  memKind.value = Number(e.detail.value) === 1 ? 'fact' : 'preference';
}

async function addMemory(): Promise<void> {
  const content = memContent.value.trim();
  if (!content || memSaving.value) return;
  memSaving.value = true;
  try {
    await butlerApi.addMemory(memKind.value, content);
    memContent.value = '';
    const r = await butlerApi.listMemories();
    memories.value = r.memories;
    toast('已保存');
  } catch (exc) {
    toast(exc instanceof ApiError ? exc.detail : '保存失败');
  } finally {
    memSaving.value = false;
  }
}

function deleteMemory(m: MemoryItem): void {
  (globalThis as any).uni?.showModal?.({
    title: '删除记忆',
    content: `确定删除这条${m.kind === 'preference' ? '偏好' : '事实'}？删除后管家将不再遵循。`,
    confirmColor: '#f87171',
    success: async (res: { confirm: boolean }) => {
      if (!res.confirm) return;
      try {
        await butlerApi.deleteMemory(m.id);
        memories.value = memories.value.filter((it) => it.id !== m.id);
      } catch (exc) {
        toast(exc instanceof ApiError ? exc.detail : '删除失败');
      }
    },
  });
}

// ---------------- 人设 ----------------

function applyPreset(p: PersonaResponse['presets'][number]): void {
  personaDraft.value = p.prompt || '';
  toast(`已选择「${p.name}」，保存后生效`);
}

async function savePersona(): Promise<void> {
  if (personaSaving.value) return;
  const text = personaDraft.value.slice(0, 1000);
  personaSaving.value = true;
  try {
    await butlerApi.updatePersona(text);
    toast('人设已保存并热生效');
  } catch (exc) {
    toast(exc instanceof ApiError ? exc.detail : '保存失败');
  } finally {
    personaSaving.value = false;
  }
}

// ---------------- Token / 审计 ----------------

const budgetPct = computed(() => {
  const b = tokenBudget.value;
  if (!b || !b.enabled || b.limit <= 0) return 0;
  return Math.min(100, Math.round((b.used_today / b.limit) * 100));
});

const budgetBarWidth = computed(() => `${Math.max(2, budgetPct.value)}%`);

const budgetFillClass = computed(() => {
  if (budgetPct.value >= 90) return 'budget-fill-danger';
  if (budgetPct.value >= 70) return 'budget-fill-warn';
  return 'budget-fill-ok';
});

const filteredAudit = computed(() => {
  if (auditFilter.value === 'all') return audit.value;
  if (auditFilter.value === 'device_app') {
    return audit.value.filter((a) => a.actor.startsWith('device_app:'));
  }
  return audit.value.filter((a) => a.actor === auditFilter.value);
});

function actorLabel(actor: string): string {
  if (actor.startsWith('device_app:')) return '本机App';
  const map: Record<string, string> = {
    agent: 'AI管家',
    web_direct: '设备直控',
    user: '用户',
    system: '系统',
  };
  return map[actor] || actor;
}

function actorClass(actor: string): string {
  if (actor.startsWith('device_app:')) return 'actor-app';
  const map: Record<string, string> = {
    agent: 'actor-agent',
    web_direct: 'actor-direct',
    user: 'actor-user',
    system: 'actor-system',
  };
  return map[actor] || 'actor-system';
}

function auditStatusClass(status: string): string {
  if (status === 'ok') return 'st-ok';
  if (status === 'error') return 'st-error';
  if (status === 'declined' || status === 'cancelled') return 'st-warn';
  return 'st-idle';
}

// ---------------- 连接与主题 ----------------

function saveCloudUrl(): void {
  auth.setCloudUrl(cloudDraft.value.trim());
  toast('云中转地址已保存，断网时将自动尝试');
}

function confirmLogout(): void {
  (globalThis as any).uni?.showModal?.({
    title: '退出登录',
    content: '将清除本机保存的设备令牌与 ID，需要重新配对后才能控制设备。',
    confirmColor: '#f87171',
    success: (res: { confirm: boolean }) => {
      if (!res.confirm) return;
      auth.logout();
      (globalThis as any).uni?.reLaunch?.({ url: '/pages/pairing/index' });
    },
  });
}
</script>

<style lang="scss" scoped>
.card {
  background: var(--bg-card);
  backdrop-filter: blur(12px);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-card);
  padding: 12px;
  margin: 8px 12px 0;
}
.mem-add {
  display: flex;
  gap: 8px;
  margin: 8px 12px 0;
  align-items: center;
}
.mem-kind-view {
  width: 92px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
}
.mem-input {
  flex: 1;
}
.mem-add-btn {
  width: 74px;
  flex-shrink: 0;
  min-height: 44px;
  line-height: 44px;
  font-size: 14px;
}
.mem-card {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-card);
  padding: 10px 12px;
  margin: 8px 12px 0;
}
.mem-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.mem-kind {
  font-size: 10px;
  border-radius: 999px;
  padding: 1px 8px;
  font-weight: 700;
}
.mem-pref {
  color: var(--brand-primary);
  background: rgba(56, 189, 248, 0.12);
}
.mem-fact {
  color: #a78bfa;
  background: rgba(167, 139, 250, 0.14);
}
.mem-source,
.mem-member {
  font-size: 11px;
  color: var(--text-secondary);
}
.mem-del {
  margin-left: auto;
  font-size: 12px;
  color: var(--status-error);
  padding: 2px 6px;
}
.mem-content {
  display: block;
  margin-top: 6px;
  font-size: 14px;
  color: var(--text-primary);
  line-height: 1.5;
}
.mem-time {
  display: block;
  margin-top: 4px;
  font-size: 10px;
  color: var(--text-secondary);
}
.preset-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}
.preset-chip {
  font-size: 12px;
  color: var(--text-secondary);
  background: rgba(148, 163, 184, 0.12);
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  padding: 6px 14px;
}
.preset-chip-active {
  color: #ffffff;
  background: var(--brand-gradient);
  border-color: transparent;
}
.persona-textarea {
  width: 100%;
  min-height: 110px;
  line-height: 1.6;
}
.persona-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
}
.persona-count {
  font-size: 11px;
  color: var(--text-secondary);
}
.persona-save {
  width: 130px;
  min-height: 40px;
  line-height: 40px;
  font-size: 13px;
}
.budget-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}
.budget-text {
  font-size: 14px;
  color: var(--text-primary);
  font-weight: 600;
}
.budget-pct {
  font-size: 14px;
  color: var(--brand-primary);
  font-weight: 700;
}
.budget-bar {
  margin-top: 8px;
  height: 10px;
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  overflow: hidden;
}
/* 进度条品牌渐变（Web .progress-fill 同源） */
.budget-fill {
  height: 100%;
  background: var(--brand-gradient);
  border-radius: 999px;
  transition: width 0.3s ease;
}
.budget-fill-ok {
  background: var(--brand-gradient);
}
.budget-fill-warn {
  background: var(--status-warn);
}
.budget-fill-danger {
  background: var(--status-error);
}
.budget-remain {
  display: block;
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-secondary);
}
.model-card {
  padding: 10px 12px;
}
.model-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.model-name {
  font-size: 12px;
  font-weight: 700;
  color: var(--brand-primary);
  background: rgba(56, 189, 248, 0.12);
  border-radius: 999px;
  padding: 2px 10px;
}
.model-value {
  font-size: 13px;
  color: var(--text-primary);
  font-weight: 600;
}
.model-muted {
  display: block;
  margin-top: 5px;
  font-size: 11px;
  color: var(--text-secondary);
  word-break: break-all;
}
.audit-filter {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.audit-filter-chip {
  font-size: 12px;
  color: var(--text-secondary);
  background: rgba(148, 163, 184, 0.12);
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  padding: 4px 12px;
}
.audit-filter-active {
  color: #ffffff;
  background: var(--brand-gradient);
  border-color: transparent;
}
.audit-card {
  padding: 10px 12px;
}
.audit-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.audit-actor {
  font-size: 10px;
  font-weight: 700;
  border-radius: 999px;
  padding: 1px 8px;
  flex-shrink: 0;
}
.actor-agent {
  color: #a78bfa;
  background: rgba(167, 139, 250, 0.15);
}
.actor-direct {
  color: var(--brand-primary);
  background: rgba(56, 189, 248, 0.13);
}
.actor-app {
  color: var(--status-ok);
  background: rgba(52, 211, 153, 0.14);
}
.actor-user {
  color: var(--text-secondary);
  background: rgba(148, 163, 184, 0.14);
}
.actor-system {
  color: var(--text-secondary);
  background: rgba(148, 163, 184, 0.1);
}
.audit-action {
  font-size: 12px;
  color: var(--text-primary);
  font-family: Menlo, Consolas, monospace;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.audit-status {
  font-size: 10px;
  flex-shrink: 0;
}
.st-ok {
  color: var(--status-ok);
}
.st-warn {
  color: var(--status-warn);
}
.st-error {
  color: var(--status-error);
}
.st-idle {
  color: var(--text-secondary);
}
.audit-detail {
  display: block;
  margin-top: 5px;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.5;
  word-break: break-all;
}
.audit-time {
  display: block;
  margin-top: 4px;
  font-size: 10px;
  color: var(--text-secondary);
}
.status-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 0;
}
.status-label {
  font-size: 13px;
  color: var(--text-secondary);
}
.status-value {
  font-size: 13px;
  color: var(--text-primary);
  font-weight: 600;
  text-align: right;
}
.status-value.small {
  font-size: 11px;
  font-weight: 500;
  word-break: break-all;
  max-width: 60%;
}
.rule-on {
  color: var(--status-ok);
}
.rule-off {
  color: var(--text-secondary);
}
.muted {
  font-size: 11px;
  color: var(--text-secondary);
  line-height: 1.6;
}
.disclaimer {
  display: block;
  margin-top: 8px;
}
.cloud-edit-row {
  display: flex;
  gap: 8px;
}
.cloud-input {
  flex: 1;
}
.cloud-save {
  width: 76px;
  flex-shrink: 0;
}
.theme-row {
  display: flex;
  gap: 10px;
}
.day-chip {
  font-size: 13px;
  color: var(--text-secondary);
  background: rgba(148, 163, 184, 0.12);
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  padding: 8px 18px;
}
.day-chip-active {
  color: #ffffff;
  background: var(--brand-gradient);
  border-color: transparent;
  font-weight: 600;
}
.logout-btn {
  margin-top: 14px;
  width: 100%;
  border-color: rgba(248, 113, 113, 0.5);
  color: var(--status-error);
}
.form-label {
  display: block;
  font-size: 12px;
  color: var(--text-secondary);
  margin: 12px 0 6px;
}
.ph {
  color: rgba(148, 163, 184, 0.55);
}
</style>
