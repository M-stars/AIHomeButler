<template>
  <view class="page-root" :class="auth.theme === 'light' ? 'theme-light' : 'theme-dark'">
    <view class="pair-wrap">
      <view class="pair-hero">
        <text class="pair-title">🛰️ 连接你的家庭管家</text>
        <text class="pair-desc">首次使用请在管家所在局域网内完成 6 位配对码绑定；凭证只保存在本机。</text>
      </view>

      <!-- Step 1：服务地址探测 -->
      <view class="pair-card">
        <view class="pair-step-head">
          <text class="pair-step-badge">1</text>
          <text class="pair-step-title">管家服务地址</text>
          <text v-if="probe.ok" class="pair-probe-ok">🟢 已连接 · v{{ probe.health?.version }} · 协议 {{ probe.health?.protocol_version }}</text>
        </view>
        <input
          v-model="lanUrl"
          class="text-input"
          placeholder="如 http://192.168.1.10:8080"
          placeholder-class="pair-placeholder"
        />
        <view class="pair-cloud-row">
          <text class="pair-cloud-label">云中转地址（可选）</text>
          <input
            v-model="cloudUrl"
            class="text-input"
            placeholder="如 https://butler.example.com"
            placeholder-class="pair-placeholder"
          />
        </view>
        <button class="primary-btn pair-btn" :disabled="probing" @click="onProbe">
          {{ probing ? '探测中…' : '探测服务' }}
        </button>
        <text v-if="probe.error" class="pair-error">❌ {{ probe.error }}</text>
      </view>

      <!-- Step 2：输入配对码 -->
      <view class="pair-card" :class="{ 'pair-card-disabled': !probe.ok }">
        <view class="pair-step-head">
          <text class="pair-step-badge">2</text>
          <text class="pair-step-title">输入 6 位配对码</text>
        </view>
        <text class="pair-step-hint">在管家 Web 管理台点击「配对新设备」生成配对码（10 分钟内有效，最多 3 次尝试）。</text>
        <input
          v-model="code"
          class="text-input pair-code-input"
          type="number"
          maxlength="6"
          placeholder="••••••"
          placeholder-class="pair-placeholder"
        />
        <input
          v-model="deviceName"
          class="text-input"
          placeholder="设备名称（如 我的手机）"
          placeholder-class="pair-placeholder"
        />
        <button class="primary-btn pair-btn" :disabled="!probe.ok || pairing || code.length !== 6" @click="onPair">
          {{ pairing ? '配对中…' : '完成配对' }}
        </button>
        <text v-if="pairError" class="pair-error">❌ {{ pairError }}</text>
      </view>

      <view class="pair-footer">
        <text class="pair-footnote">配对成功后本机将持有独立设备令牌（bh_ 前缀），管理员可随时在管理台踢除。</text>
      </view>
    </view>
  </view>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue';
import { useAuthStore } from '@/stores/auth';
import { probeHealthAt } from '@/services/connection';
import { configureHttp } from '@/services/http';
import * as butlerApi from '@/services/butlerApi';
import { detectPlatform } from '@/services/environment';
import { STORAGE_KEYS, setString } from '@/services/storage';
import { startRealtime } from '@/services/realtimeBinding';
import type { ProbeResult } from '@/services/connection';

const auth = useAuthStore();

const lanUrl = ref(auth.lanUrl || '');
const cloudUrl = ref(auth.cloudUrl || '');
const deviceName = ref('');
const code = ref('');
const probing = ref(false);
const pairing = ref(false);
const probe = reactive<ProbeResult>({ ok: false, error: '' });
const pairError = ref('');

async function onProbe(): Promise<void> {
  probing.value = true;
  probe.ok = false;
  probe.error = '';
  try {
    const result = await probeHealthAt(lanUrl.value, 1500);
    probe.ok = result.ok;
    probe.health = result.health;
    probe.error = result.error || '';
    if (result.ok) {
      // 云地址同步保存（可选通道，探测失败不阻塞配对）
      auth.setLanUrl(lanUrl.value.trim());
      if (cloudUrl.value.trim()) {
        auth.setCloudUrl(cloudUrl.value.trim());
      }
    }
  } finally {
    probing.value = false;
  }
}

async function onPair(): Promise<void> {
  if (pairing.value) return;
  pairError.value = '';
  pairing.value = true;
  try {
    const base = lanUrl.value.trim();
    configureHttp(base, '');
    const res = await butlerApi.pairConfirm(code.value.trim(), deviceName.value.trim() || '我的手机', detectPlatform());
    // 持久化凭证（绝不打印明文令牌）
    setString(STORAGE_KEYS.LAN_URL, base);
    if (cloudUrl.value.trim()) {
      setString(STORAGE_KEYS.CLOUD_URL, cloudUrl.value.trim());
    }
    auth.setCredentials({
      token: res.token,
      deviceId: res.device_id,
      deviceName: res.device_name,
      baseUrl: base,
    });
    auth.setLanUrl(base);
    await auth.refreshSystemInfo();
    // 关键：首次配对不走 App.vue onLaunch 的启动分支，实时层必须在此拉起
    // （SSE 确认推送/轮询兜底、流水线事件、device_updated 都依赖它）
    startRealtime();
    (globalThis as any).uni?.showToast?.({ title: '配对成功', icon: 'success' });
    setTimeout(() => {
      (globalThis as any).uni?.reLaunch?.({ url: '/pages/home/index' });
    }, 600);
  } catch (exc) {
    const detail = (exc as { detail?: string }).detail;
    pairError.value = detail || '配对失败，请检查配对码与服务地址';
  } finally {
    pairing.value = false;
  }
}
</script>

<style lang="scss" scoped>
.pair-wrap {
  padding: 20px 16px 40px;
}
.pair-hero {
  margin-bottom: 16px;
}
.pair-title {
  display: block;
  font-size: 22px;
  font-weight: 800;
  color: var(--text-primary);
}
.pair-desc {
  display: block;
  margin-top: 8px;
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.6;
}
.pair-card {
  background: var(--bg-card);
  backdrop-filter: blur(12px);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-card);
  padding: 14px;
  margin-bottom: 14px;
}
.pair-card-disabled {
  opacity: 0.55;
}
.pair-step-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.pair-step-badge {
  width: 22px;
  height: 22px;
  line-height: 22px;
  text-align: center;
  border-radius: 50%;
  background: var(--brand-gradient);
  color: #ffffff;
  font-size: 13px;
  font-weight: 700;
  flex-shrink: 0;
}
.pair-step-title {
  font-size: 15px;
  font-weight: 700;
  color: var(--text-primary);
}
.pair-probe-ok {
  font-size: 12px;
  color: var(--status-ok);
}
.pair-step-hint {
  display: block;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.6;
  margin-bottom: 10px;
}
.pair-cloud-row {
  margin-top: 10px;
}
.pair-cloud-label {
  display: block;
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 6px;
}
.pair-btn {
  margin-top: 12px;
  width: 100%;
}
.pair-code-input {
  font-size: 26px;
  letter-spacing: 10px;
  text-align: center;
  font-weight: 700;
  margin-bottom: 10px;
}
.pair-error {
  display: block;
  margin-top: 10px;
  font-size: 12px;
  color: var(--status-error);
  line-height: 1.5;
}
.pair-footer {
  margin-top: 6px;
}
.pair-footnote {
  font-size: 11px;
  color: var(--text-secondary);
  line-height: 1.6;
}
.pair-placeholder {
  color: rgba(148, 163, 184, 0.55);
}
</style>
