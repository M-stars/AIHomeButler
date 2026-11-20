<template>
  <view
    class="dc-card glass-card"
    :class="[spanClass, { 'dc-card-on': isOn, 'dc-card-disabled': !device.available }]"
  >
    <view class="dc-head">
      <view class="dc-icon-wrap">
        <text class="dc-icon">{{ typeIcon }}</text>
        <view class="dc-meta">
          <text class="dc-name">{{ device.name }}</text>
          <text class="dc-id mono">{{ device.id }}</text>
        </view>
      </view>
      <text v-if="device.area" class="dc-area">{{ device.area }}</text>
    </view>

    <view v-if="!device.available" class="dc-badges">
      <text class="dc-badge-offline">离线</text>
    </view>

    <view class="dc-controls">
      <!-- 开关行：light / fan / switch / climate / humidifier -->
      <view v-if="hasToggle" class="dc-row dc-row-main">
      <text class="dc-state-text">{{ stateText }}</text>
      <switch
        :checked="local.on"
        color="#38bdf8"
        :disabled="busy || !device.available"
        style="transform: scale(0.82);"
        @change="onToggle"
      />
    </view>

    <!-- light 亮度滑条：仅在松手（change）时发请求，防连续触发 3s 防重放 -->
    <view v-if="has('set_brightness')" class="dc-slider-row">
      <text class="dc-slider-label">亮度 {{ Math.round(local.brightness) }}%</text>
      <slider
        class="dc-slider"
        :min="1"
        :max="100"
        :step="1"
        :value="Math.round(local.brightness)"
        :disabled="busy || !device.available"
        activeColor="#38bdf8"
        backgroundColor="rgba(148,163,184,0.3)"
        :block-size="20"
        @change="onBrightnessChange"
      />
    </view>

    <!-- fan 三档风速 -->
    <view v-if="has('set_speed')" class="dc-seg">
      <view
        v-for="s in SPEEDS"
        :key="s"
        class="dc-seg-item"
        :class="{ 'dc-seg-active': local.speed === s }"
        @click="onSpeed(s)"
      >
        <text>{{ speedLabel(s) }}</text>
      </view>
    </view>

    <!-- cover 三键控制栏 + 位置滑条（100=全开） -->
    <view v-if="device.type === 'cover'" class="dc-btn-row">
      <button v-if="has('open')" class="dc-btn" :disabled="busy || !device.available" @click="run('open')">开</button>
      <button v-if="has('stop')" class="dc-btn" :disabled="busy || !device.available" @click="run('stop')">停</button>
      <button v-if="has('close')" class="dc-btn" :disabled="busy || !device.available" @click="run('close')">关</button>
    </view>
    <view v-if="device.type === 'cover' && has('set_position')" class="dc-slider-row">
      <text class="dc-slider-label">开度 {{ Math.round(local.position) }}%</text>
      <slider
        class="dc-slider"
        :min="0"
        :max="100"
        :step="1"
        :value="Math.round(local.position)"
        :disabled="busy || !device.available"
        activeColor="#38bdf8"
        backgroundColor="rgba(148,163,184,0.3)"
        :block-size="20"
        @change="onPositionChange"
      />
    </view>

    <!-- climate：当前温度 + 目标温度步进器 -->
    <view v-if="device.type === 'climate'" class="dc-climate">
      <text class="dc-current">当前 {{ formatNullable(currentTemp) }}°C</text>
      <view v-if="has('set_temperature')" class="dc-stepper">
        <button class="dc-step-btn" :disabled="busy || !device.available" @click="stepTemp(-1)">−</button>
        <text class="dc-target">{{ Math.round(local.temperature) }}°C</text>
        <button class="dc-step-btn" :disabled="busy || !device.available" @click="stepTemp(1)">＋</button>
      </view>
    </view>

    <!-- humidifier：当前湿度 + 目标湿度滑条（0-100%） -->
    <view v-if="device.type === 'humidifier'">
      <text class="dc-current">当前 {{ formatNullable(currentHumidity) }}%</text>
      <view v-if="has('set_humidity')" class="dc-slider-row">
        <text class="dc-slider-label">目标 {{ Math.round(local.humidity) }}%</text>
        <slider
          class="dc-slider"
          :min="0"
          :max="100"
          :step="1"
          :value="Math.round(local.humidity)"
          :disabled="busy || !device.available"
          activeColor="#38bdf8"
          backgroundColor="rgba(148,163,184,0.3)"
          :block-size="20"
          @change="onHumidityChange"
        />
      </view>
    </view>

    <!-- vacuum：状态 + 电池 + 按钮组 -->
      <view v-if="device.type === 'vacuum'" class="dc-vacuum">
        <text class="dc-current">状态 {{ statusText }} · 电量 {{ formatNullable(battery) }}%</text>
        <view class="dc-btn-row">
          <button v-if="has('start')" class="dc-btn" :disabled="busy || !device.available" @click="run('start')">开始</button>
          <button v-if="has('pause')" class="dc-btn" :disabled="busy || !device.available" @click="run('pause')">暂停</button>
          <button v-if="has('stop')" class="dc-btn" :disabled="busy || !device.available" @click="run('stop')">停止</button>
          <button v-if="has('return_to_base')" class="dc-btn" :disabled="busy || !device.available" @click="run('return_to_base')">回充</button>
        </view>
      </view>
    </view>
  </view>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import { useDevicesStore, type CommandFeedback } from '@/stores/devices';
import { hapticFeedback } from '@/services/environment';
import { ApiError } from '@/services/http';
import type { DeviceView } from '@/types/api';

const props = defineProps<{ device: DeviceView }>();

const SPEEDS = ['low', 'medium', 'high'] as const;

const store = useDevicesStore();

/** 请求中锁（防连点 + 防交互期间状态同步打断）。 */
const busyFlag = ref(0);
const busy = computed(() => busyFlag.value > 0);

// 控件本地态：从设备状态同步；交互期间不被外部更新打断
const local = reactive({
  on: false,
  brightness: 100,
  speed: 'medium' as 'low' | 'medium' | 'high',
  position: 0,
  temperature: 26,
  humidity: 50,
});

function syncFromState(): void {
  const st = props.device.state;
  if (!st) return;
  local.on = st.on === true;
  local.brightness = clampNumber(st.brightness, 1, 100, 100);
  local.speed = st.speed === 'low' || st.speed === 'high' ? st.speed : 'medium';
  local.position = clampNumber(st.position, 0, 100, 0);
  local.temperature = clampNumber(st.temperature, 16, 30, 26);
  local.humidity = clampNumber(st.humidity, 0, 100, 50);
}

function clampNumber(v: unknown, min: number, max: number, fallback: number): number {
  const n = typeof v === 'number' && isFinite(v) ? v : Number(v);
  if (!isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function formatNullable(v: unknown): string {
  return v === null || v === undefined ? '--' : String(Math.round(Number(v) * 10) / 10);
}

watch(() => props.device, syncFromState, { immediate: true, deep: false });

const has = (action: string): boolean => props.device.actions.includes(action);

const hasToggle = computed(() => has('turn_on') && has('turn_off'));

const isOn = computed(() => {
  if (props.device.type === 'cover') {
    return props.device.state?.open === true;
  }
  return local.on;
});

const stateText = computed(() => {
  const st = props.device.state;
  if (props.device.type === 'cover') {
    return st?.open === true ? '已打开' : '已关闭';
  }
  if (props.device.type === 'climate') {
    return local.on ? `运行中（${st?.hvac_mode || 'auto'}）` : '已关闭';
  }
  return local.on ? '开启' : '关闭';
});

const statusText = computed(() => String(props.device.state?.status || '空闲'));
const currentTemp = computed(() => props.device.state?.current_temperature ?? null);
const currentHumidity = computed(() => props.device.state?.current_humidity ?? null);
const battery = computed(() => props.device.state?.battery ?? null);

const spanClass = computed(() => {
  const t = props.device.type;
  return t === 'cover' || t === 'climate' || t === 'vacuum' ? 'dc-span2' : 'dc-span1';
});

/** 设备类型图标（对齐 Web 控制台 device-icon 方块）。 */
const typeIcon = computed(() => {
  const map: Record<string, string> = {
    light: '💡',
    fan: '🪭',
    switch: '🔌',
    cover: '🪟',
    climate: '❄️',
    humidifier: '💧',
    vacuum: '🤖',
    sensor: '🌡️',
  };
  return map[props.device.type] || '🏠';
});

function speedLabel(s: 'low' | 'medium' | 'high'): string {
  return s === 'low' ? '低速' : s === 'medium' ? '中速' : '高速';
}

function toast(title: string): void {
  (globalThis as any).uni?.showToast?.({ title, icon: 'none', duration: 2500 });
}

/** 统一反馈呈现（§3.2 直控回读反馈规则）。 */
function handleFeedback(fb: CommandFeedback): void {
  if (fb.kind === 'ok') {
    toast(fb.message);
  } else if (fb.kind === 'duplicate') {
    toast(fb.message);
  } else if (fb.kind === 'unknown') {
    (globalThis as any).uni?.showModal?.({
      title: '⚠️ 回读状态未知',
      content: fb.message,
      showCancel: false,
      confirmText: '知道了',
    });
  } else {
    toast(fb.message);
  }
}

/** 发起直控并做完整反馈闭环；失败（400/403 等）回滚控件状态。 */
async function run(action: string, value?: string | number): Promise<void> {
  if (busyFlag.value > 0) return;
  busyFlag.value += 1;
  try {
    const fb = await store.directCommand(props.device.id, action, value);
    handleFeedback(fb);
    syncFromState();
  } catch (exc) {
    // HTTP 400/403：回滚开关/滑条 UI，展示服务端 detail
    syncFromState();
    toast(exc instanceof ApiError ? exc.detail : '操作失败，请稍后重试');
  } finally {
    busyFlag.value -= 1;
  }
}

function onToggle(e: any): void {
  const next = e?.detail?.value === true;
  hapticFeedback();
  if (next === local.on) return;
  run(next ? 'turn_on' : 'turn_off');
}

/** 松手才发（uni slider @change 在拖动结束时触发）。 */
function onBrightnessChange(e: { detail: { value: number } }): void {
  const v = Math.round(e.detail.value);
  local.brightness = v;
  run('set_brightness', v);
}

function onPositionChange(e: { detail: { value: number } }): void {
  const v = Math.round(e.detail.value);
  local.position = v;
  run('set_position', v);
}

function onHumidityChange(e: { detail: { value: number } }): void {
  const v = Math.round(e.detail.value);
  local.humidity = v;
  run('set_humidity', v);
}

function onSpeed(s: 'low' | 'medium' | 'high'): void {
  hapticFeedback();
  if (local.speed === s) return;
  local.speed = s;
  run('set_speed', s);
}

function stepTemp(delta: number): void {
  const next = Math.min(30, Math.max(16, Math.round(local.temperature) + delta));
  if (next === local.temperature) return;
  local.temperature = next;
  run('set_temperature', next);
}
</script>

<style lang="scss" scoped>
/* 控制区分隔线 + 内嵌行（对齐 Web .device-controls/.control-row） */
.dc-controls {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.dc-card {
  padding: 12px;
  display: flex;
  flex-direction: column;
}
.dc-span1 {
  grid-column: span 1;
}
.dc-span2 {
  grid-column: span 2;
}
/* 开启态：青蓝渐变底 + 边框（Web .device-card.is-on 同源） */
.dc-card-on {
  border-color: rgba(56, 189, 248, 0.45);
  background: linear-gradient(160deg, rgba(56, 189, 248, 0.1), var(--bg-card));
}
.dc-card-disabled {
  opacity: 0.62;
}
.dc-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 6px;
}
.dc-icon-wrap {
  display: flex;
  align-items: center;
  gap: 9px;
  flex: 1;
  min-width: 0;
}
.dc-icon {
  width: 36px;
  height: 36px;
  line-height: 36px;
  text-align: center;
  font-size: 17px;
  border-radius: 10px;
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.dc-meta {
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.dc-name {
  font-size: 14px;
  font-weight: 700;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dc-id {
  font-size: 10px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dc-area {
  font-size: 11px;
  color: var(--text-secondary);
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  padding: 2px 8px;
  flex-shrink: 0;
}
.dc-badges {
  margin-top: 6px;
}
.dc-badge-offline {
  font-size: 10px;
  color: var(--status-error);
  border: 1px solid rgba(248, 113, 113, 0.4);
  background: var(--status-error-bg);
  border-radius: 999px;
  padding: 1px 7px;
}
.dc-row-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 40px;
}
.dc-state-text {
  font-size: 13px;
  color: var(--text-secondary);
}
.dc-slider-row {
  margin-top: 2px;
}
.dc-slider-label {
  font-size: 12px;
  color: var(--text-secondary);
}
.dc-slider {
  margin: 4px 0 0;
  width: 100%;
}
.dc-seg {
  display: inline-flex;
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 2px;
  gap: 2px;
}
.dc-seg-item {
  flex: 1;
  text-align: center;
  font-size: 12px;
  color: var(--text-secondary);
  padding: 5px 0;
  min-height: 20px;
  min-width: 44px;
  border-radius: 6px;
  font-weight: 600;
}
/* 分段选中 = 品牌色底深色字（Web .seg-btn.active 同源） */
.dc-seg-active {
  background: var(--brand-primary);
  color: #08101e;
  font-weight: 700;
}
.dc-btn-row {
  display: flex;
  gap: 8px;
}
.dc-btn {
  flex: 1;
  min-height: 44px;
  line-height: 40px;
  font-size: 13px;
  padding: 0 4px;
  margin: 0;
  background: rgba(56, 189, 248, 0.14);
  color: var(--brand-primary);
  border-radius: var(--radius-control);
  border: 1px solid rgba(56, 189, 248, 0.3);
}
.dc-btn::after {
  border: none;
}
.dc-btn[disabled] {
  opacity: 0.45;
}
.dc-climate {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.dc-current {
  font-size: 12px;
  color: var(--text-secondary);
  display: block;
}
.dc-stepper {
  display: flex;
  align-items: center;
  gap: 10px;
  justify-content: center;
}
.dc-step-btn {
  width: 44px;
  height: 44px;
  line-height: 44px;
  padding: 0;
  margin: 0;
  font-size: 20px;
  background: var(--bg-input);
  border: 1px solid var(--border-subtle);
  color: var(--text-primary);
  border-radius: var(--radius-control);
}
.dc-step-btn::after {
  border: none;
}
.dc-target {
  font-size: 20px;
  font-weight: 700;
  color: var(--brand-primary);
  min-width: 64px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}
.dc-vacuum {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
</style>
