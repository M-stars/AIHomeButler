/**
 * 设备列表与直控 store（MOBILE_APP_SPEC.md §7 stores/devices.ts）。
 *
 * 直控回读反馈规则（§3.2 第 4 条，验收清单第 4/5 项）：
 *   - ok && verified=true        → 应用 state_after，提示「已执行并验证」；
 *   - stage=duplicate_skipped    → 提示「3 秒内已执行过相同操作」；
 *   - status=unknown             → 黄色告警「指令可能已发送，但回读状态未知」，
 *                                  绝不渲染为成功；
 *   - HTTP 400/403               → 回滚开关 UI 状态，展示服务端 detail。
 *
 * 防重放配合（§5）：开关类控件 400ms 请求中锁；滑条只在松手时发请求（组件层）。
 */
import { defineStore } from 'pinia';
import * as butlerApi from '@/services/butlerApi';
import { ApiError } from '@/services/http';
import type { DeviceState, DeviceView, DirectCommandResponse } from '@/types/api';

export interface CommandFeedback {
  kind: 'ok' | 'duplicate' | 'unknown' | 'error';
  message: string;
}

/** 控件请求锁时长（毫秒）：避免连点触发服务端 3s 防重放。 */
const COMMAND_LOCK_MS = 400;

export const useDevicesStore = defineStore('devices', {
  state: () => ({
    devices: [] as DeviceView[],
    loadedAt: 0,
    loading: false,
    loadError: '',
    /** 设备 id → 上次指令发起时间戳（400ms 防连点锁）。 */
    commandLocks: {} as Record<string, number>,
  }),

  getters: {
    /** 传感器子集（SensorStrip 横向条）。 */
    sensorDevices(state): DeviceView[] {
      return state.devices.filter((d) => d.type === 'sensor');
    },
    /** 可控设备子集（渲染直控卡片网格）。 */
    controllableDevices(state): DeviceView[] {
      return state.devices.filter((d) => d.type !== 'sensor');
    },
    /** 去重区域清单（区域筛选胶囊，[全部, ...areas]）。 */
    areas(state): string[] {
      const set = new Set<string>();
      state.devices.forEach((d) => {
        if (d.area) set.add(d.area);
      });
      return ['全部', ...Array.from(set)];
    },
    isCommandLocked(state) {
      return (deviceId: string): boolean => {
        const last = state.commandLocks[deviceId] || 0;
        return Date.now() - last < COMMAND_LOCK_MS;
      };
    },
  },

  actions: {
    async refresh(): Promise<void> {
      this.loading = true;
      this.loadError = '';
      try {
        const res = await butlerApi.getDevices();
        this.devices = res.devices;
        this.loadedAt = Date.now();
      } catch (exc) {
        this.loadError = exc instanceof Error ? exc.message : '设备列表加载失败';
      } finally {
        this.loading = false;
      }
    },

    /** 仅刷新单台设备最新状态（直控 unknown 后下拉确认用）。 */
    async refreshOne(deviceId: string): Promise<void> {
      try {
        const view = await butlerApi.getDeviceState(deviceId);
        this.applyDeviceView(view);
      } catch {
        /* 单台刷新失败静默，保留旧状态 */
      }
    },

    applyDeviceView(view: DeviceView): void {
      const idx = this.devices.findIndex((d) => d.id === view.id);
      if (idx >= 0) {
        this.devices.splice(idx, 1, view);
      } else {
        this.devices.push(view);
      }
    },

    /** SSE device_updated 事件：本地先落地，避免整表刷新闪烁。 */
    applyDeviceUpdated(payload: {
      device_id: string;
      action: string;
      status: string;
      stage?: string;
      state: DeviceState | null;
    }): void {
      const target = this.devices.find((d) => d.id === payload.device_id);
      if (target && payload.state) {
        target.state = payload.state;
      }
    },

    markLocked(deviceId: string): boolean {
      const last = this.commandLocks[deviceId] || 0;
      if (Date.now() - last < COMMAND_LOCK_MS) {
        return false;
      }
      this.commandLocks[deviceId] = Date.now();
      return true;
    },

    /**
     * 设备直控完整闭环：400ms 防连点锁 → 发指令 → 应用回读 → 产出反馈。
     * 抛出的错误由组件层回滚控件状态（store 不在失败时改状态）。
     */
    async directCommand(
      deviceId: string,
      action: string,
      value?: string | number,
    ): Promise<CommandFeedback> {
      if (!this.markLocked(deviceId)) {
        return { kind: 'duplicate', message: '3 秒内已执行过相同操作' };
      }
      let res: DirectCommandResponse;
      try {
        res = await butlerApi.sendDeviceCommand(deviceId, action, value);
      } catch (exc) {
        if (exc instanceof ApiError) {
          throw exc; // 400/403：组件回滚 UI 并展示 detail
        }
        throw exc;
      }
      return this.applyCommandResult(res);
    },

    /** 把服务端直控响应翻译为用户反馈，并同步 state_after。 */
    applyCommandResult(res: DirectCommandResponse): CommandFeedback {
      const target = this.devices.find((d) => d.id === res.device_id);
      if (res.status === 'ok' && res.verified === true) {
        if (target && res.state_after) {
          target.state = res.state_after;
        }
        return { kind: 'ok', message: '已执行并验证' };
      }
      if (res.stage === 'duplicate_skipped') {
        return { kind: 'duplicate', message: '3 秒内已执行过相同操作' };
      }
      if (res.status === 'unknown') {
        // 诚实语义（仓库红线 3）：unknown 绝不渲染为成功
        return {
          kind: 'unknown',
          message: '指令可能已发送，但回读状态未知，请下拉刷新确认',
        };
      }
      if (res.status === 'ok') {
        if (target && res.state_after) {
          target.state = res.state_after;
        }
        return { kind: 'ok', message: res.message || '已执行' };
      }
      return { kind: 'error', message: res.message || '执行失败' };
    },
  },
});
