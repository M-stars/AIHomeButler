<template>
  <view class="ss-wrap">
    <scroll-view scroll-x class="ss-scroll" show-scrollbar="false">
      <view class="ss-row">
        <view
          v-for="card in cards"
          :key="card.key"
          class="ss-card"
          :class="{ 'ss-card-offline': card.offline }"
        >
          <view class="ss-card-head">
            <text class="ss-name">{{ card.name }}</text>
            <text v-if="card.offline" class="ss-badge ss-badge-offline">离线</text>
            <text v-else-if="card.stale" class="ss-badge ss-badge-stale">数据陈旧</text>
          </view>
          <view v-if="card.readings.length === 0" class="ss-none">
            <text>暂无读数</text>
          </view>
          <view v-for="(r, i) in card.readings" :key="i" class="ss-reading">
            <text class="ss-label">{{ r.label }}</text>
            <text class="ss-value">{{ r.display }}</text>
          </view>
          <text v-if="card.sampledAt" class="ss-time">{{ card.sampledAt }}</text>
        </view>
        <view v-if="cards.length === 0" class="ss-none ss-none-wide">
          <text>暂无传感器设备</text>
        </view>
      </view>
    </scroll-view>
  </view>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useDevicesStore } from '@/stores/devices';
import { extractSensorReadings, sensorSampledAt } from '@/services/sensor';

const devices = useDevicesStore();

interface SensorCard {
  key: string;
  name: string;
  offline: boolean;
  stale: boolean;
  sampledAt: string;
  readings: Array<{ label: string; display: string; badge: string | null }>;
}

/**
 * 顶部环境传感器横向卡片条（§3.2 第 2 条）。
 * 字段兼容读取规则集中在 services/sensor.ts，此处只做展示。
 */
const cards = computed<SensorCard[]>(() =>
  devices.sensorDevices.map((d) => {
    const readings = extractSensorReadings(d);
    return {
      key: d.id,
      name: d.name,
      offline: d.available === false,
      stale: d.state?.fresh === false && d.available !== false,
      sampledAt: sensorSampledAt(d),
      readings,
    };
  }),
);
</script>

<style lang="scss" scoped>
.ss-wrap {
  margin: 8px 0 4px;
}
.ss-scroll {
  white-space: nowrap;
}
.ss-row {
  display: inline-flex;
  gap: 10px;
  padding: 0 12px;
}
.ss-card {
  display: inline-flex;
  flex-direction: column;
  min-width: 150px;
  max-width: 220px;
  background: var(--bg-card);
  backdrop-filter: blur(12px);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-card);
  padding: 10px 12px;
  margin-right: 0;
}
.ss-card-offline {
  opacity: 0.62;
}
.ss-card-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}
.ss-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 110px;
}
.ss-badge {
  font-size: 10px;
  border-radius: 999px;
  padding: 1px 7px;
  flex-shrink: 0;
}
.ss-badge-stale {
  color: var(--status-warn);
  background: var(--status-warn-bg);
  border: 1px solid rgba(251, 191, 36, 0.4);
}
.ss-badge-offline {
  color: var(--status-error);
  background: var(--status-error-bg);
  border: 1px solid rgba(248, 113, 113, 0.4);
}
.ss-reading {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-top: 3px;
}
.ss-label {
  font-size: 12px;
  color: var(--text-secondary);
}
/* 读数大字品牌色（Web .device-reading 同源） */
.ss-value {
  font-size: 18px;
  font-weight: 700;
  color: var(--brand-primary);
  font-variant-numeric: tabular-nums;
}
.ss-none {
  font-size: 12px;
  color: var(--text-secondary);
}
.ss-none-wide {
  padding: 12px 4px;
}
.ss-time {
  margin-top: 6px;
  font-size: 10px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
