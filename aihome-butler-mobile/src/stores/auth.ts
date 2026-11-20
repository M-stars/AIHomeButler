/**
 * 认证与连接状态 store（MOBILE_APP_SPEC.md §7 stores/auth.ts）。
 *
 * 职责：连接地址（LAN/Cloud）、device_id/token、双通道状态机、
 * 系统能力位（capabilities）、主题切换；401 统一收敛跳配对页。
 */
import { defineStore } from 'pinia';
import {
  STORAGE_KEYS,
  getString,
  setString,
  clearCredentials as storageClearCredentials,
} from '@/services/storage';
import { configureHttp, onUnauthorized } from '@/services/http';
import {
  resolveChannel,
  type Channel,
  type ChannelDecision,
} from '@/services/connection';
import * as butlerApi from '@/services/butlerApi';
import { useConfirmationStore } from '@/stores/confirmation';
import type { SystemInfoResponse } from '@/types/api';

export type ThemeMode = 'dark' | 'light';

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: '',
    deviceId: '',
    deviceName: '',
    lanUrl: '',
    cloudUrl: '',
    channel: 'offline' as Channel,
    activeBaseUrl: '',
    channelReason: '',
    systemInfo: null as SystemInfoResponse | null,
    /** App 启动引导是否已完成（决定启动页去向）。 */
    bootstrapped: false,
    needsPairing: false,
    theme: 'dark' as ThemeMode,
  }),

  getters: {
    capabilities(state): SystemInfoResponse['capabilities'] | null {
      return state.systemInfo ? state.systemInfo.capabilities : null;
    },
    backend(state): string {
      return state.systemInfo ? state.systemInfo.backend : '';
    },
    /** 连接状态胶囊文案（首页顶部状态栏）。 */
    channelLabel(state): string {
      if (state.channel === 'lan') return '🟢 局域网直连';
      if (state.channel === 'cloud') return '🔵 云中转';
      return '🔴 已断开';
    },
    breaker(state) {
      return state.systemInfo ? state.systemInfo.ha_circuit_breaker : null;
    },
  },

  actions: {
    /** App 启动引导：读凭证 → 双通道探测 → 激活通道 → 拉系统能力。 */
    async bootstrap(): Promise<ChannelDecision> {
      this.token = getString(STORAGE_KEYS.TOKEN);
      this.deviceId = getString(STORAGE_KEYS.DEVICE_ID);
      this.deviceName = getString(STORAGE_KEYS.DEVICE_NAME);
      this.lanUrl = getString(STORAGE_KEYS.LAN_URL);
      this.cloudUrl = getString(STORAGE_KEYS.CLOUD_URL);
      const storedTheme = getString(STORAGE_KEYS.THEME);
      this.theme = storedTheme === 'light' ? 'light' : 'dark';
      // 启动即按持久化主题同步导航栏（onLaunch 的那次调用发生在读取之前）
      this.applyNavigationBarTheme();

      // 无论是否有 token 都先注册 401 收敛（http 层在收到 401 时回调）
      onUnauthorized(() => this.handleUnauthorized());

      const decision = await resolveChannel({
        hasToken: Boolean(this.token),
        lanUrl: this.lanUrl,
        cloudUrl: this.cloudUrl,
      });
      this.applyDecision(decision);
      if (decision.channel !== 'offline') {
        await this.refreshSystemInfo();
      }
      this.bootstrapped = true;
      return decision;
    },

    applyDecision(decision: ChannelDecision): void {
      this.channel = decision.channel;
      this.activeBaseUrl = decision.activeBaseUrl;
      this.channelReason = decision.reason;
      this.needsPairing = decision.channel === 'offline' && !this.token;
      configureHttp(decision.activeBaseUrl, this.token);
    },

    /** 手动重试双通道切换（离线页/首页刷新按钮）。 */
    async retryConnect(): Promise<ChannelDecision> {
      const decision = await resolveChannel({
        hasToken: Boolean(this.token),
        lanUrl: this.lanUrl,
        cloudUrl: this.cloudUrl,
      });
      this.applyDecision(decision);
      if (decision.channel !== 'offline') {
        await this.refreshSystemInfo();
      }
      return decision;
    },

    async refreshSystemInfo(): Promise<void> {
      try {
        this.systemInfo = await butlerApi.getSystemInfo();
      } catch {
        // 系统能力拉取失败不改变通道状态：下次刷新再试
        this.systemInfo = null;
      }
    },

    /** 配对成功后写入凭证并激活连接。 */
    setCredentials(creds: { token: string; deviceId: string; deviceName: string; baseUrl: string }): void {
      this.token = creds.token;
      this.deviceId = creds.deviceId;
      this.deviceName = creds.deviceName;
      this.activeBaseUrl = creds.baseUrl;
      this.channel = 'lan';
      this.channelReason = '局域网直连';
      this.needsPairing = false;
      setString(STORAGE_KEYS.TOKEN, creds.token);
      setString(STORAGE_KEYS.DEVICE_ID, creds.deviceId);
      setString(STORAGE_KEYS.DEVICE_NAME, creds.deviceName);
      configureHttp(creds.baseUrl, creds.token);
    },

    setLanUrl(url: string): void {
      this.lanUrl = url;
      setString(STORAGE_KEYS.LAN_URL, url);
    },

    setCloudUrl(url: string): void {
      this.cloudUrl = url;
      setString(STORAGE_KEYS.CLOUD_URL, url);
    },

    setTheme(theme: ThemeMode): void {
      this.theme = theme;
      setString(STORAGE_KEYS.THEME, theme);
      this.applyNavigationBarTheme();
    },

    toggleTheme(): void {
      this.setTheme(this.theme === 'dark' ? 'light' : 'dark');
    },

    /** 同步导航栏颜色到主题（失败静默，部分端不支持动态改色）。 */
    applyNavigationBarTheme(): void {
      try {
        (globalThis as any).uni?.setNavigationBarColor?.({
          frontColor: this.theme === 'dark' ? '#ffffff' : '#000000',
          backgroundColor: this.theme === 'dark' ? '#090e1a' : '#f1f5f9',
          fail: () => undefined,
        });
      } catch {
        /* 忽略 */
      }
      // H5：body 底色跟随主题，避免内容不足一屏时露出暗色底
      try {
        if (typeof document !== 'undefined') {
          document.body.style.backgroundColor = this.theme === 'dark' ? '#090e1a' : '#f1f5f9';
        }
      } catch {
        /* 非 H5 端无 document */
      }
    },

    /** 退出并清除本地凭证（保留连接地址便于重新配对）。 */
    logout(): void {
      storageClearCredentials();
      this.token = '';
      this.deviceId = '';
      this.deviceName = '';
      this.systemInfo = null;
      this.channel = 'offline';
      this.activeBaseUrl = '';
      this.channelReason = '已退出登录';
      this.needsPairing = true;
      configureHttp('', '');
      this.stopRealtimeSafely();
    },

    /** 401 统一收敛：凭证已失效或被管理员踢除（§2.3）。 */
    handleUnauthorized(): void {
      const hadToken = Boolean(this.token);
      storageClearCredentials();
      this.token = '';
      this.deviceId = '';
      this.systemInfo = null;
      this.channel = 'offline';
      this.activeBaseUrl = '';
      this.channelReason = '设备凭证已失效';
      // 实时层停止与确认队列清理不允许阻断跳转收敛
      try {
        this.stopRealtimeSafely();
      } catch (exc) {
        console.error('[auth] stopRealtimeSafely 失败', exc);
      }
      if (hadToken) {
        try {
          (globalThis as any).uni?.showToast?.({
            title: '设备凭证已失效或被管理员解绑，请重新配对',
            icon: 'none',
            duration: 2500,
          });
        } catch {
          /* toast 失败不影响跳转 */
        }
        (globalThis as any).uni?.reLaunch?.({
          url: '/pages/pairing/index',
          fail: (err: unknown) => console.error('[auth] 401 跳转配对页失败', err),
        });
      }
    },

    /** 停止实时层并清空确认队列（动态 import 避免 store ↔ binding 循环依赖）。 */
    stopRealtimeSafely(): void {
      import('@/services/realtimeBinding')
        .then((m) => m.stopRealtime())
        .catch(() => undefined);
      useConfirmationStore().clear();
    },
  },
});
