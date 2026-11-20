/**
 * 设备直控 store 反馈语义测试（MOBILE_APP_SPEC.md §3.2 第 4 条 /
 * 验收清单第 4、5 项）：verified 成功 / 3s 防重放 / unknown 诚实告警。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { installUniMock, resetHttp, setupPinia, type UniMock } from './helpers';

vi.mock('@/services/butlerApi', () => ({
  getDevices: vi.fn(),
  getDeviceState: vi.fn(),
  sendDeviceCommand: vi.fn(),
}));

import * as butlerApi from '@/services/butlerApi';
import { useDevicesStore } from '@/stores/devices';
import type { DeviceView } from '@/types/api';

let mock: UniMock;

function lightDevice(): DeviceView {
  return {
    id: 'light.a',
    name: '客厅灯',
    type: 'light',
    area: '客厅',
    available: true,
    actions: ['turn_on', 'turn_off', 'set_brightness'],
    state: { on: false, brightness: 80 },
  };
}

beforeEach(async () => {
  mock = installUniMock();
  await resetHttp();
  await setupPinia();
  vi.clearAllMocks();
});

describe('refresh / SSE 更新', () => {
  it('refresh 拉取全量设备', async () => {
    const store = useDevicesStore();
    vi.mocked(butlerApi.getDevices).mockResolvedValue({ devices: [lightDevice()] });
    await store.refresh();
    expect(store.controllableDevices).toHaveLength(1);
    expect(store.sensorDevices).toHaveLength(0);
    expect(store.areas).toEqual(['全部', '客厅']);
  });

  it('applyDeviceUpdated 仅更新目标设备状态', async () => {
    const store = useDevicesStore();
    vi.mocked(butlerApi.getDevices).mockResolvedValue({ devices: [lightDevice()] });
    await store.refresh();
    store.applyDeviceUpdated({
      device_id: 'light.a',
      action: 'turn_on',
      status: 'ok',
      state: { on: true },
    });
    expect(store.devices[0].state?.on).toBe(true);
  });
});

describe('directCommand 反馈语义', () => {
  it('ok + verified → 应用 state_after 并提示"已执行并验证"', async () => {
    const store = useDevicesStore();
    vi.mocked(butlerApi.getDevices).mockResolvedValue({ devices: [lightDevice()] });
    await store.refresh();
    vi.mocked(butlerApi.sendDeviceCommand).mockResolvedValue({
      device_id: 'light.a',
      action: 'turn_on',
      status: 'ok',
      stage: 'verified',
      message: '执行成功并回读验证',
      verified: true,
      state_after: { on: true, brightness: 80 },
    });
    const fb = await store.directCommand('light.a', 'turn_on');
    expect(fb.kind).toBe('ok');
    expect(fb.message).toBe('已执行并验证');
    expect(store.devices[0].state?.on).toBe(true);
  });

  it('duplicate_skipped → "3 秒内已执行过相同操作"', async () => {
    const store = useDevicesStore();
    vi.mocked(butlerApi.getDevices).mockResolvedValue({ devices: [lightDevice()] });
    await store.refresh();
    vi.mocked(butlerApi.sendDeviceCommand).mockResolvedValue({
      device_id: 'light.a',
      action: 'turn_on',
      status: 'ok',
      stage: 'duplicate_skipped',
      message: '3 秒窗口内重复指令已跳过',
      verified: false,
      state_after: null,
    });
    const fb = await store.directCommand('light.a', 'turn_on');
    expect(fb.kind).toBe('duplicate');
    expect(fb.message).toContain('3 秒内已执行过相同操作');
  });

  it('unknown → 黄色告警语义，绝不渲染为成功', async () => {
    const store = useDevicesStore();
    vi.mocked(butlerApi.getDevices).mockResolvedValue({ devices: [lightDevice()] });
    await store.refresh();
    vi.mocked(butlerApi.sendDeviceCommand).mockResolvedValue({
      device_id: 'light.a',
      action: 'turn_on',
      status: 'unknown',
      stage: 'unknown',
      message: '指令可能已发送，但回读状态未知',
      verified: false,
      state_after: null,
    });
    const fb = await store.directCommand('light.a', 'turn_on');
    expect(fb.kind).toBe('unknown');
    expect(fb.message).toContain('回读状态未知');
    // 状态保持原样（不把 unknown 当成功渲染）
    expect(store.devices[0].state?.on).toBe(false);
  });

  it('400ms 防连点锁：窗口内第二次调用直接拒绝', async () => {
    const store = useDevicesStore();
    vi.mocked(butlerApi.getDevices).mockResolvedValue({ devices: [lightDevice()] });
    await store.refresh();
    vi.mocked(butlerApi.sendDeviceCommand).mockResolvedValue({
      device_id: 'light.a',
      action: 'turn_on',
      status: 'ok',
      stage: 'verified',
      message: '',
      verified: true,
      state_after: { on: true },
    });
    const first = await store.directCommand('light.a', 'turn_on');
    expect(first.kind).toBe('ok');
    const second = await store.directCommand('light.a', 'turn_off');
    expect(second.kind).toBe('duplicate');
    expect(butlerApi.sendDeviceCommand).toHaveBeenCalledTimes(1);
  });
});
