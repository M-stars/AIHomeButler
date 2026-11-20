<template>
  <view class="page-root" :class="auth.theme === 'light' ? 'theme-light' : 'theme-dark'">
    <!-- 顶部状态栏：品牌方块 + 标题 + 连接胶囊 + 后端徽章 + 刷新 -->
    <view class="top-bar">
      <view class="top-left">
        <view class="brand-logo">
          <text>🛰️</text>
        </view>
        <text class="top-title">AI 家庭管家</text>
        <text class="channel-pill" :class="channelClass">{{ auth.channelLabel }}</text>
      </view>
      <view class="top-right">
        <text v-if="auth.backend" class="backend-badge">{{ backendBadge }}</text>
        <text class="refresh-btn" @click="manualRefresh">⟳</text>
      </view>
    </view>

    <!-- HA 熔断告警横幅 -->
    <view v-if="breakerOpen" class="breaker-banner">
      <text>⚠️ Home Assistant 熔断保护中，约 {{ retryAfter }}s 后自动恢复探测</text>
    </view>

    <!-- 离线告警 + 手动重试 -->
    <view v-if="auth.channel === 'offline'" class="offline-banner" @click="manualRefresh">
      <text>🔴 与管家服务失去连接，点击重试（局域网 → 云端自动切换）</text>
    </view>

    <!-- 环境传感器横向卡片条 -->
    <sensor-strip v-if="devices.sensorDevices.length > 0" />

    <!-- 区域筛选胶囊 -->
    <view v-if="devices.controllableDevices.length > 0" class="area-bar">
      <scroll-view scroll-x class="area-scroll" show-scrollbar="false">
        <view class="area-row">
          <view
            v-for="area in devices.areas"
            :key="area"
            class="area-pill"
            :class="{ 'area-pill-active': area === activeArea }"
            @click="activeArea = area"
          >
            <text>{{ area }}</text>
          </view>
        </view>
      </scroll-view>
    </view>

    <!-- 动态能力设备卡片网格（2 列自适应） -->
    <view class="device-grid">
      <device-card v-for="d in filteredDevices" :key="d.id" :device="d" />
    </view>

    <view v-if="devices.loadError && devices.devices.length === 0" class="empty-hint">
      <text>{{ devices.loadError }}</text>
    </view>
    <view v-else-if="filteredDevices.length === 0 && devices.controllableDevices.length === 0" class="empty-hint">
      <text>未发现可控设备。若使用 HA 后端请确认自动发现已开启；模拟后端默认有 3 台可控设备。</text>
    </view>

    <global-confirm-sheet />
  </view>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { onPullDownRefresh, onShow } from '@dcloudio/uni-app';
import { useAuthStore } from '@/stores/auth';
import { useDevicesStore } from '@/stores/devices';
import { useConfirmationStore } from '@/stores/confirmation';
import SensorStrip from '@/components/SensorStrip.vue';
import DeviceCard from '@/components/DeviceCard.vue';
import GlobalConfirmSheet from '@/components/GlobalConfirmSheet.vue';

const auth = useAuthStore();
const devices = useDevicesStore();
const confirmations = useConfirmationStore();
const activeArea = ref('全部');

const channelClass = computed(() => {
  if (auth.channel === 'lan') return 'channel-lan';
  if (auth.channel === 'cloud') return 'channel-cloud';
  return 'channel-off';
});

const backendBadge = computed(() => {
  const map: Record<string, string> = {
    homeassistant: 'HA',
    gpio: 'GPIO',
    simulated: 'SIM',
  };
  return map[auth.backend] || auth.backend;
});

const breakerOpen = computed(() => auth.breaker?.state === 'open');
const retryAfter = computed(() => (auth.breaker as any)?.retry_after_seconds ?? '?');

const filteredDevices = computed(() => {
  if (activeArea.value === '全部') return devices.controllableDevices;
  return devices.controllableDevices.filter((d) => d.area === activeArea.value);
});

onShow(() => {
  if (auth.token) {
    devices.refresh();
    // 全局审批抽屉的数据兜底（SSE 未连接时靠轮询）
    confirmations.load();
  }
});

onPullDownRefresh(async () => {
  await Promise.all([devices.refresh(), confirmations.load(), auth.refreshSystemInfo()]);
  (globalThis as any).uni?.stopPullDownRefresh?.();
});

async function manualRefresh(): Promise<void> {
  if (auth.channel === 'offline') {
    const decision = await auth.retryConnect();
    if (decision.channel !== 'offline') {
      (globalThis as any).uni?.showToast?.({ title: decision.reason, icon: 'none' });
    } else {
      (globalThis as any).uni?.showToast?.({ title: '仍不可达，请检查管家服务', icon: 'none' });
    }
  }
  devices.refresh();
  confirmations.load();
}
</script>

<style lang="scss" scoped>
.top-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px 4px;
}
.top-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
/* 品牌方块（Web .brand-logo 同源） */
.brand-logo {
  width: 34px;
  height: 34px;
  border-radius: 11px;
  background: var(--brand-gradient);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 17px;
  box-shadow: 0 6px 18px rgba(56, 189, 248, 0.35);
  flex-shrink: 0;
}
.top-title {
  font-size: 17px;
  font-weight: 700;
  color: var(--text-primary);
}
.channel-pill {
  font-size: 11px;
  border-radius: 999px;
  padding: 3px 9px;
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}
.channel-lan {
  color: var(--status-ok);
  background: rgba(52, 211, 153, 0.12);
  border-color: rgba(52, 211, 153, 0.4);
}
.channel-cloud {
  color: var(--brand-primary);
  background: rgba(56, 189, 248, 0.12);
  border-color: rgba(56, 189, 248, 0.4);
}
.channel-off {
  color: var(--status-error);
  background: rgba(248, 113, 113, 0.12);
  border-color: rgba(248, 113, 113, 0.4);
}
.top-right {
  display: flex;
  align-items: center;
  gap: 8px;
}
.backend-badge {
  font-size: 11px;
  font-weight: 700;
  color: var(--brand-primary);
  background: rgba(56, 189, 248, 0.12);
  border: 1px solid rgba(56, 189, 248, 0.35);
  border-radius: 999px;
  padding: 3px 9px;
}
.refresh-btn {
  width: 34px;
  height: 34px;
  line-height: 34px;
  text-align: center;
  font-size: 17px;
  color: var(--text-secondary);
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 50%;
}
.breaker-banner {
  margin: 10px 12px 0;
  padding: 10px 12px;
  border-radius: var(--radius-control);
  background: rgba(249, 115, 22, 0.16);
  border: 1px solid rgba(249, 115, 22, 0.55);
  color: #fb923c;
  font-size: 12px;
  line-height: 1.5;
}
.offline-banner {
  margin: 10px 12px 0;
  padding: 10px 12px;
  border-radius: var(--radius-control);
  background: rgba(248, 113, 113, 0.14);
  border: 1px solid rgba(248, 113, 113, 0.5);
  color: var(--status-error);
  font-size: 12px;
  line-height: 1.5;
}
.area-bar {
  margin-top: 4px;
}
.area-scroll {
  white-space: nowrap;
}
.area-row {
  display: inline-flex;
  gap: 8px;
  padding: 6px 12px 2px;
}
.area-pill {
  font-size: 12px;
  color: var(--text-secondary);
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  padding: 5px 14px;
}
.area-pill-active {
  color: #ffffff;
  background: var(--brand-gradient);
  border-color: transparent;
  font-weight: 600;
}
.device-grid {
  margin: 10px 12px 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
</style>
