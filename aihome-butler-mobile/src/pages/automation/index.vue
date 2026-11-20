<template>
  <view class="page-root" :class="auth.theme === 'light' ? 'theme-light' : 'theme-dark'">
    <!-- 三段式 Segmented Control -->
    <view class="auto-seg">
      <view class="seg-item" :class="{ 'seg-active': pane === 'routines' }" @click="switchPane('routines')">
        <text>例行任务</text>
      </view>
      <view class="seg-item" :class="{ 'seg-active': pane === 'alerts' }" @click="switchPane('alerts')">
        <text>提醒与规则</text>
      </view>
      <view
        v-if="householdEnabled"
        class="seg-item"
        :class="{ 'seg-active': pane === 'household' }"
        @click="switchPane('household')"
      >
        <text>生活台账</text>
      </view>
    </view>

    <!-- ============ 例行任务 ============ -->
    <view v-if="pane === 'routines'">
      <view v-for="r in routines" :key="r.id" class="routine-card">
        <view class="routine-head">
          <text class="routine-name">{{ r.name }}</text>
          <switch
            :checked="r.enabled"
            color="#38bdf8"
            style="transform: scale(0.75);"
            @change="onToggleRoutine(r)"
          />
        </view>
        <view class="routine-meta">
          <text>⏰ {{ r.schedule?.time }} · {{ describeDays(r.schedule?.days) }}</text>
        </view>
        <view class="routine-actions">
          <text v-for="(step, i) in r.actions" :key="i" class="routine-action">{{ stepSummary(step) }}</text>
        </view>
        <view class="routine-foot">
          <text class="routine-result" :class="{ 'routine-result-bad': isBadResult(r.last_result) }">
            {{ describeLastResult(r.last_result) }}
          </text>
          <text class="routine-del" @click="onDeleteRoutine(r)">删除</text>
        </view>
      </view>
      <view v-if="routines.length === 0" class="empty-hint">
        <text>还没有例行任务。创建预授权任务后，到点自动执行完整动作清单（创建即授权，执行不再逐次确认）。</text>
      </view>
      <button class="primary-btn fab-btn" @click="openCreate">＋ 新建例行任务</button>
    </view>

    <!-- ============ 提醒与规则 ============ -->
    <view v-if="pane === 'alerts'">
      <text class="section-title">主动提醒</text>
      <view v-for="n in notifications" :key="n.id" class="notif-card" :class="notifClass(n.level)">
        <view class="notif-head">
          <text class="notif-rule">{{ n.rule_name || '系统提醒' }}</text>
          <text class="notif-time">{{ formatTime(n.created_at) }}</text>
        </view>
        <text class="notif-msg">{{ n.message }}</text>
      </view>
      <view v-if="notifications.length === 0" class="empty-hint"><text>暂无提醒记录。</text></view>

      <text class="section-title">确定性规则（只读）</text>
      <view v-for="rule in rules" :key="rule.id" class="rule-card">
        <view class="rule-head">
          <text class="rule-name">{{ rule.name }}</text>
          <text class="rule-kind">{{ ruleKindLabel(rule.kind) }}</text>
          <text class="rule-state" :class="rule.enabled ? 'rule-on' : 'rule-off'">
            {{ rule.enabled ? '启用中' : '已停用' }}
          </text>
        </view>
      </view>
      <view v-if="rules.length === 0" class="empty-hint"><text>暂无监测规则。</text></view>
    </view>

    <!-- ============ 生活台账 ============ -->
    <view v-if="pane === 'household'">
      <text class="section-title">食材库存（按剩余天数升序）</text>
      <view v-for="item in pantry" :key="item.id" class="pantry-card" :class="pantryClass(item)">
        <view class="pantry-head">
          <text class="pantry-name">{{ item.name }}</text>
          <text class="pantry-left" :class="pantryClass(item)">{{ pantryLeftLabel(item) }}</text>
        </view>
        <text class="pantry-meta">{{ item.category }} · 数量 {{ item.quantity }} · 入库 {{ item.added_date }}</text>
      </view>
      <view v-if="pantry.length === 0" class="empty-hint"><text>库存为空，可通过对话录入食材。</text></view>

      <text class="section-title">衣柜台账</text>
      <view v-for="g in wardrobe" :key="g.id" class="wardrobe-card">
        <view class="pantry-head">
          <text class="pantry-name">{{ g.name }}</text>
          <text class="pantry-left">{{ g.owner }}</text>
        </view>
        <text class="pantry-meta">
          {{ g.category }} · 保暖度 {{ g.warmth }} · 穿着 {{ g.times_worn_since_wash }} 次
          · 洗涤间隔 {{ g.wash_interval_days ?? '--' }} 天
          <text v-if="g.status && g.status !== 'clean'"> · {{ g.status }}</text>
        </text>
      </view>
      <view v-if="wardrobe.length === 0" class="empty-hint"><text>衣柜台账为空，可通过对话记账。</text></view>
    </view>

    <!-- ============ 新建例行任务底部弹窗 ============ -->
    <view v-if="createVisible" class="popup-mask" @click="createVisible = false">
      <view class="popup-sheet" @click.stop>
        <view class="popup-title">
          <text>新建例行任务</text>
          <text class="popup-close" @click="createVisible = false">✕</text>
        </view>

        <text class="form-label">任务名</text>
        <input v-model="form.name" class="text-input" placeholder="如 周末晨起窗帘" placeholder-class="ph" />

        <text class="form-label">执行时间</text>
        <picker mode="time" :value="form.time" @change="onTimeChange">
          <view class="text-input picker-view"><text>{{ form.time || '选择 HH:MM' }}</text></view>
        </picker>

        <text class="form-label">重复周期</text>
        <view class="days-presets">
          <view class="day-chip" :class="{ 'day-chip-active': daysPreset === 'daily' }" @click="pickPreset('daily')"><text>每天</text></view>
          <view class="day-chip" :class="{ 'day-chip-active': daysPreset === 'weekdays' }" @click="pickPreset('weekdays')"><text>工作日</text></view>
          <view class="day-chip" :class="{ 'day-chip-active': daysPreset === 'weekend' }" @click="pickPreset('weekend')"><text>周末</text></view>
          <view class="day-chip" :class="{ 'day-chip-active': daysPreset === 'custom' }" @click="daysPreset = 'custom'"><text>自定义</text></view>
        </view>
        <view v-if="daysPreset === 'custom'" class="days-custom">
          <view
            v-for="(label, idx) in WEEKDAY_LABELS"
            :key="idx"
            class="day-chip"
            :class="{ 'day-chip-active': customDays.includes(idx) }"
            @click="toggleCustomDay(idx)"
          >
            <text>{{ label }}</text>
          </view>
        </view>

        <text class="form-label">动作清单（最多 10 步，创建即授权）</text>
        <view v-for="(step, i) in form.actions" :key="i" class="step-row">
          <text class="step-text">{{ stepSummary(step) }}</text>
          <text class="step-del" @click="removeStep(i)">移除</text>
        </view>
        <view class="step-builder">
          <picker mode="selector" :range="deviceNames" @change="onDevicePicked">
            <view class="text-input picker-view"><text>{{ builderDeviceName || '选择设备' }}</text></view>
          </picker>
          <picker
            v-if="builderActions.length > 0"
            mode="selector"
            :range="builderActionLabels"
            @change="onActionPicked"
          >
            <view class="text-input picker-view"><text>{{ builder.action || '选择动作' }}</text></view>
          </picker>
          <input
            v-if="needsValue(builder.action)"
            v-model="builder.value"
            class="text-input value-input"
            type="number"
            :placeholder="valueHint(builder.action)"
            placeholder-class="ph"
          />
          <button class="ghost-btn add-step-btn" :disabled="!canAddStep" @click="addStep">＋ 添加</button>
        </view>

        <view class="notify-row">
          <text class="form-label notify-label">执行成功后通知我</text>
          <switch :checked="form.notify_on_success" color="#38bdf8" style="transform: scale(0.75);" @change="onNotifyChange" />
        </view>

        <button class="primary-btn" :disabled="!canSubmit" @click="submitRoutine">
          {{ submitting ? '创建中…' : '创建（授权以上动作清单）' }}
        </button>
        <text v-if="formError" class="form-error">❌ {{ formError }}</text>
        <view class="popup-safe-area" />
      </view>
    </view>

    <global-confirm-sheet />
  </view>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue';
import { onShow } from '@dcloudio/uni-app';
import { useAuthStore } from '@/stores/auth';
import { useDevicesStore } from '@/stores/devices';
import { useConfirmationStore } from '@/stores/confirmation';
import * as butlerApi from '@/services/butlerApi';
import { ApiError } from '@/services/http';
import {
  WEEKDAY_LABELS,
  daysToApiValue,
  describeDays,
  describeLastResult,
  describeRoutineAction,
} from '@/services/routines';
import GlobalConfirmSheet from '@/components/GlobalConfirmSheet.vue';
import type {
  NotificationItem,
  PantryItem,
  ProactiveRuleItem,
  RoutineItem,
  GarmentItem,
} from '@/types/api';

const auth = useAuthStore();
const devices = useDevicesStore();
const confirmations = useConfirmationStore();

type Pane = 'routines' | 'alerts' | 'household';
const pane = ref<Pane>('routines');

const routines = ref<RoutineItem[]>([]);
const notifications = ref<NotificationItem[]>([]);
const rules = ref<ProactiveRuleItem[]>([]);
const pantry = ref<PantryItem[]>([]);
const wardrobe = ref<GarmentItem[]>([]);

const householdEnabled = computed(() => auth.capabilities?.household === true);

onShow(() => {
  confirmations.load();
  refreshPane();
  if (devices.devices.length === 0 && auth.token) {
    devices.refresh();
  }
});

async function refreshPane(): Promise<void> {
  try {
    if (pane.value === 'routines') {
      const res = await butlerApi.listRoutines();
      routines.value = res.routines;
    } else if (pane.value === 'alerts') {
      const [n, r] = await Promise.all([butlerApi.listNotifications(), butlerApi.listRules()]);
      notifications.value = n.notifications;
      rules.value = r.rules;
    } else if (pane.value === 'household') {
      const [p, w] = await Promise.all([butlerApi.getPantry(), butlerApi.getWardrobe()]);
      pantry.value = p.items;
      wardrobe.value = w.garments;
    }
  } catch (exc) {
    // 台账未装配（503）等：只提示不打断
    const detail = exc instanceof ApiError ? exc.detail : '';
    if (detail) toast(detail);
  }
}

function switchPane(p: Pane): void {
  pane.value = p;
  refreshPane();
}

function toast(title: string): void {
  (globalThis as any).uni?.showToast?.({ title, icon: 'none', duration: 2500 });
}

function formatTime(iso: string): string {
  return (iso || '').replace('T', ' ').slice(5, 16);
}

// ---------------- 例行任务 ----------------

function stepSummary(step: { device_id: string; action: string; value?: unknown }): string {
  return describeRoutineAction(step, (id) => devices.devices.find((d) => d.id === id)?.name || '');
}

function isBadResult(result: string | null | undefined): boolean {
  const text = (result || '').toLowerCase();
  return text.includes('error') || text.includes('失败') || text.includes('unknown');
}

async function onToggleRoutine(r: RoutineItem): Promise<void> {
  try {
    const res = await butlerApi.toggleRoutine(r.id);
    r.enabled = res.enabled;
  } catch (exc) {
    toast(exc instanceof ApiError ? exc.detail : '切换失败');
  }
}

function onDeleteRoutine(r: RoutineItem): void {
  (globalThis as any).uni?.showModal?.({
    title: '删除例行任务',
    content: `确定删除「${r.name}」？删除后到点不再执行。`,
    confirmColor: '#f87171',
    success: async (res: { confirm: boolean }) => {
      if (!res.confirm) return;
      try {
        await butlerApi.deleteRoutine(r.id);
        routines.value = routines.value.filter((it) => it.id !== r.id);
        toast('已删除');
      } catch (exc) {
        toast(exc instanceof ApiError ? exc.detail : '删除失败');
      }
    },
  });
}

// ---------------- 新建表单 ----------------

const createVisible = ref(false);
const submitting = ref(false);
const formError = ref('');
const form = reactive({
  name: '',
  time: '',
  notify_on_success: false,
  actions: [] as Array<{ device_id: string; action: string; value?: string | number }>,
});
const daysPreset = ref<'daily' | 'weekdays' | 'weekend' | 'custom'>('daily');
const customDays = ref<number[]>([]);
const builder = reactive({ deviceIdx: -1, action: '', value: '' });

function openCreate(): void {
  form.name = '';
  form.time = '';
  form.actions = [];
  form.notify_on_success = false;
  daysPreset.value = 'daily';
  customDays.value = [];
  builder.deviceIdx = -1;
  builder.action = '';
  builder.value = '';
  formError.value = '';
  createVisible.value = true;
  if (devices.devices.length === 0) {
    devices.refresh();
  }
}

const deviceNames = computed(() =>
  devices.controllableDevices.map((d) => `${d.name}${d.area ? '（' + d.area + '）' : ''}`),
);
const builderDevice = computed(() =>
  builder.deviceIdx >= 0 ? devices.controllableDevices[builder.deviceIdx] : null,
);
const builderDeviceName = computed(() => builderDevice.value?.name || '');
const builderActions = computed(() => builderDevice.value?.actions || []);
const builderActionLabels = computed(() => builderActions.value.map((a) => actionLabel(a)));

function actionLabel(action: string): string {
  const map: Record<string, string> = {
    turn_on: '打开',
    turn_off: '关闭',
    set_brightness: '设亮度(1-100)',
    set_speed: '设风速',
    open: '打开(全开)',
    stop: '停止',
    close: '关闭(全关)',
    set_position: '设位置(0-100)',
    set_temperature: '设温度',
    set_humidity: '设湿度(0-100)',
    start: '开始清扫',
    pause: '暂停',
    return_to_base: '回充',
  };
  return map[action] || action;
}

function needsValue(action: string): boolean {
  return ['set_brightness', 'set_position', 'set_temperature', 'set_humidity'].includes(action);
}

function valueHint(action: string): string {
  const map: Record<string, string> = {
    set_brightness: '亮度 1-100',
    set_position: '位置 0-100（100=全开）',
    set_temperature: '目标温度 °C',
    set_humidity: '目标湿度 0-100',
  };
  return map[action] || '';
}

const canAddStep = computed(() => {
  const dev = builderDevice.value;
  if (!dev || !builder.action) return false;
  if (needsValue(builder.action)) {
    return builder.value !== '' && !isNaN(Number(builder.value));
  }
  return true;
});

function onDevicePicked(e: { detail: { value: number | string } }): void {
  builder.deviceIdx = Number(e.detail.value);
  builder.action = '';
  builder.value = '';
}

function onActionPicked(e: { detail: { value: number | string } }): void {
  builder.action = builderActions.value[Number(e.detail.value)] || '';
  builder.value = '';
}

function addStep(): void {
  if (!canAddStep.value || form.actions.length >= 10) return;
  const step: { device_id: string; action: string; value?: string | number } = {
    device_id: builderDevice.value!.id,
    action: builder.action,
  };
  if (needsValue(builder.action)) {
    step.value = Number(builder.value);
  }
  form.actions.push(step);
  builder.action = '';
  builder.value = '';
}

function removeStep(idx: number): void {
  form.actions.splice(idx, 1);
}

function onTimeChange(e: { detail: { value: string } }): void {
  form.time = e.detail.value;
}

function pickPreset(p: 'daily' | 'weekdays' | 'weekend'): void {
  daysPreset.value = p;
}

function toggleCustomDay(day: number): void {
  const idx = customDays.value.indexOf(day);
  if (idx >= 0) {
    customDays.value.splice(idx, 1);
  } else {
    customDays.value.push(day);
  }
}

function onNotifyChange(e: any): void {
  form.notify_on_success = e?.detail?.value === true;
}

/**
 * days 严格传 daily/weekdays/weekend 或整数数组（绝不传 weekends）。
 * 自定义空选集视为 daily，避免提交非法 days。
 */
const daysApiValue = computed(() => {
  if (daysPreset.value === 'custom') {
    return daysToApiValue(customDays.value);
  }
  return daysPreset.value;
});

const canSubmit = computed(() => {
  return (
    !submitting.value &&
    form.name.trim().length > 0 &&
    /^([01]\d|2[0-3]):[0-5]\d$/.test(form.time) &&
    form.actions.length > 0 &&
    (daysPreset.value !== 'custom' || customDays.value.length > 0)
  );
});

async function submitRoutine(): Promise<void> {
  if (!canSubmit.value) return;
  submitting.value = true;
  formError.value = '';
  try {
    const days = daysApiValue.value;
    const res = await butlerApi.createRoutine({
      name: form.name.trim(),
      time: form.time,
      days: days === 'daily' ? 'daily' : days,
      actions: form.actions,
      notify_on_success: form.notify_on_success,
    } as any);
    toast(`已创建（${res.steps?.length ?? form.actions.length} 步预检通过）`);
    createVisible.value = false;
    const list = await butlerApi.listRoutines();
    routines.value = list.routines;
  } catch (exc) {
    formError.value = exc instanceof ApiError ? exc.detail : '创建失败，请稍后重试';
  } finally {
    submitting.value = false;
  }
}

// ---------------- 提醒与规则 ----------------

function notifClass(level: string): string {
  if (level === 'warning') return 'notif-warn';
  if (level === 'error') return 'notif-error';
  return 'notif-info';
}

function ruleKindLabel(kind: string): string {
  const map: Record<string, string> = {
    sensor_threshold: '阈值监测',
    device_offline: '离线监测',
    schedule: '定时提醒',
  };
  return map[kind] || kind;
}

// ---------------- 生活台账 ----------------

function pantryClass(item: PantryItem): string {
  if (item.expired) return 'pantry-expired';
  if (item.days_left !== null && item.days_left <= 2) return 'pantry-near';
  return '';
}

function pantryLeftLabel(item: PantryItem): string {
  if (item.days_left === null) return '无期限';
  if (item.days_left < 0) return `已过期 ${-item.days_left} 天`;
  if (item.days_left === 0) return '今天到期';
  return `剩 ${item.days_left} 天`;
}
</script>

<style lang="scss" scoped>
.auto-seg {
  display: flex;
  margin: 10px 12px 4px;
  background: rgba(148, 163, 184, 0.14);
  border-radius: var(--radius-control);
  overflow: hidden;
}
.seg-item {
  flex: 1;
  text-align: center;
  font-size: 13px;
  color: var(--text-secondary);
  padding: 10px 0;
}
.seg-active {
  background: var(--brand-gradient);
  color: #ffffff;
  font-weight: 700;
}
.routine-card,
.notif-card,
.rule-card,
.pantry-card,
.wardrobe-card {
  background: var(--bg-card);
  backdrop-filter: blur(12px);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-card);
  padding: 12px;
  margin: 8px 12px 0;
}
.routine-head,
.pantry-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.routine-name,
.pantry-name {
  font-size: 15px;
  font-weight: 700;
  color: var(--text-primary);
}
.routine-meta {
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}
.routine-actions {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.routine-action {
  font-size: 11px;
  color: var(--brand-primary);
  background: rgba(56, 189, 248, 0.1);
  border: 1px solid rgba(56, 189, 248, 0.28);
  border-radius: 999px;
  padding: 3px 10px;
}
.routine-foot {
  margin-top: 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.routine-result {
  font-size: 12px;
  color: var(--text-secondary);
  flex: 1;
  margin-right: 8px;
}
.routine-result-bad {
  color: var(--status-error);
}
.routine-del {
  font-size: 12px;
  color: var(--status-error);
  padding: 4px 8px;
}
.fab-btn {
  margin: 16px 12px 0;
  width: calc(100% - 24px);
}
.notif-head {
  display: flex;
  justify-content: space-between;
  margin-bottom: 4px;
}
.notif-rule {
  font-size: 13px;
  font-weight: 700;
  color: var(--text-primary);
}
.notif-time {
  font-size: 11px;
  color: var(--text-secondary);
}
.notif-msg {
  font-size: 13px;
  color: var(--text-primary);
  line-height: 1.5;
}
.notif-info {
  border-left: 3px solid var(--brand-primary);
}
.notif-warn {
  border-left: 3px solid var(--status-warn);
}
.notif-error {
  border-left: 3px solid var(--status-error);
}
.rule-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.rule-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  flex: 1;
}
.rule-kind {
  font-size: 11px;
  color: var(--text-secondary);
  background: rgba(148, 163, 184, 0.12);
  border-radius: 999px;
  padding: 2px 8px;
}
.rule-state {
  font-size: 11px;
}
.rule-on {
  color: var(--status-ok);
}
.rule-off {
  color: var(--text-secondary);
}
.pantry-left {
  font-size: 12px;
  font-weight: 700;
  color: var(--status-ok);
}
.pantry-near .pantry-left {
  color: var(--status-warn);
}
.pantry-near {
  border-color: rgba(251, 191, 36, 0.5);
}
.pantry-expired .pantry-left {
  color: var(--status-error);
}
.pantry-expired {
  border-color: rgba(248, 113, 113, 0.55);
}
.pantry-meta {
  margin-top: 5px;
  font-size: 11px;
  color: var(--text-secondary);
  line-height: 1.6;
}
/* 新建弹窗 */
.popup-mask {
  position: fixed;
  left: 0;
  right: 0;
  top: 0;
  bottom: 0;
  background: rgba(2, 6, 23, 0.6);
  z-index: 900;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
}
.popup-sheet {
  background: var(--bg-page);
  border-top: 1px solid var(--border-subtle);
  border-top-left-radius: 20px;
  border-top-right-radius: 20px;
  padding: 16px 16px 8px;
  max-height: 86vh;
  overflow-y: auto;
}
.popup-title {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 16px;
  font-weight: 800;
  color: var(--text-primary);
  margin-bottom: 10px;
}
.popup-close {
  font-size: 16px;
  color: var(--text-secondary);
  padding: 4px 8px;
}
.form-label {
  display: block;
  font-size: 12px;
  color: var(--text-secondary);
  margin: 12px 0 6px;
}
.picker-view {
  display: flex;
  align-items: center;
}
.days-presets,
.days-custom {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.days-custom {
  margin-top: 8px;
}
.day-chip {
  font-size: 12px;
  color: var(--text-secondary);
  background: rgba(148, 163, 184, 0.12);
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  padding: 6px 14px;
  min-height: 20px;
}
.day-chip-active {
  color: #ffffff;
  background: var(--brand-gradient);
  border-color: transparent;
  font-weight: 600;
}
.step-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-control);
  padding: 8px 10px;
  margin-bottom: 6px;
}
.step-text {
  font-size: 13px;
  color: var(--text-primary);
  flex: 1;
}
.step-del {
  font-size: 12px;
  color: var(--status-error);
  padding: 2px 6px;
}
.step-builder {
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: rgba(148, 163, 184, 0.08);
  border-radius: var(--radius-control);
  padding: 10px;
}
.value-input {
  width: 100%;
}
.add-step-btn {
  width: 100%;
}
.notify-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 12px;
}
.notify-label {
  margin: 0;
}
.primary-btn {
  margin-top: 16px;
  width: 100%;
}
.form-error {
  display: block;
  margin-top: 10px;
  font-size: 12px;
  color: var(--status-error);
  line-height: 1.5;
}
.ph {
  color: rgba(148, 163, 184, 0.55);
}
.popup-safe-area {
  padding-bottom: constant(safe-area-inset-bottom);
  padding-bottom: env(safe-area-inset-bottom);
  height: 8px;
}
</style>
