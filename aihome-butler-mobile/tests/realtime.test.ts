/**
 * 实时层测试（MOBILE_APP_SPEC.md §5.1）：
 * SSE 帧解析、断线退避序列、轮询节奏（活跃 1s / 空闲 4s）、前台切换。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { installUniMock, resetHttp, type UniMock } from './helpers';
import {
  parseSseBuffer,
  POLL_ACTIVE_MS,
  POLL_IDLE_MS,
  RealtimeClient,
  SSE_BACKOFF_STEPS,
} from '@/services/realtime';
import type { ConfirmationItem } from '@/types/api';

let mock: UniMock;

beforeEach(async () => {
  mock = installUniMock();
  await resetHttp();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

function makeClient(overrides: Partial<{ fetchConfirmations: any; fetchDevices: any }> = {}) {
  const fetchConfirmations = overrides.fetchConfirmations
    ?? vi.fn().mockResolvedValue({ confirmations: [] as ConfirmationItem[] });
  const client = new RealtimeClient({ fetchConfirmations });
  return { client, fetchConfirmations };
}

describe('parseSseBuffer', () => {
  it('解析命名事件与 JSON 数据，丢弃注释心跳帧', () => {
    const text = [
      'event: pipeline_step',
      'data: {"trace_id":"abc","step":"execute","status":"ok"}',
      '',
      'event: confirmation_pending',
      'data: {"id":"a1"}',
      '',
      ': heartbeat',  // 注释帧（无 data）应被丢弃
      '',
      '',
    ].join('\n');
    const { events, rest } = parseSseBuffer(text);
    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({ name: 'pipeline_step', data: { trace_id: 'abc' } });
    expect(events[1]).toMatchObject({ name: 'confirmation_pending', data: { id: 'a1' } });
    expect(rest).toBe('');
  });

  it('跨 chunk 半帧缓冲', () => {
    const first = parseSseBuffer('event: device_updated\ndata: {"devi');
    expect(first.events).toHaveLength(0);
    const second = parseSseBuffer(first.rest + 'ce_id":"light.a"}\n\n');
    expect(second.events).toHaveLength(1);
    expect(second.events[0].data.device_id).toBe('light.a');
  });

  it('非法 JSON 跳过不抛错', () => {
    const { events } = parseSseBuffer('event: x\ndata: {bad}\n\n');
    expect(events).toHaveLength(0);
  });
});

describe('轮询节奏（小程序分级短轮询）', () => {
  it('空闲 4s / 活跃 1s 切换', async () => {
    const { client, fetchConfirmations } = makeClient();
    client.start({ onEvent: vi.fn(), onConfirmations: vi.fn() });

    await vi.advanceTimersByTimeAsync(0); // 立即首轮
    expect(fetchConfirmations).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(POLL_IDLE_MS);
    expect(fetchConfirmations).toHaveBeenCalledTimes(2);

    // 对话请求进行中：节奏提到 1s
    client.setActive(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchConfirmations).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(POLL_ACTIVE_MS);
    expect(fetchConfirmations).toHaveBeenCalledTimes(4);

    client.setActive(false);
    client.stop();
  });

  it('stop 后不再轮询', async () => {
    const { client, fetchConfirmations } = makeClient();
    client.start({ onEvent: vi.fn(), onConfirmations: vi.fn() });
    await vi.advanceTimersByTimeAsync(0);
    client.stop();
    await vi.advanceTimersByTimeAsync(60000);
    expect(fetchConfirmations).toHaveBeenCalledTimes(1);
  });

  it('切后台暂停，回前台恢复并触发刷新钩子', async () => {
    const { client, fetchConfirmations } = makeClient();
    const onForegroundRefresh = vi.fn();
    client.start({ onEvent: vi.fn(), onConfirmations: vi.fn(), onForegroundRefresh });
    client.setForeground(false);
    await vi.advanceTimersByTimeAsync(60000);
    expect(fetchConfirmations).not.toHaveBeenCalled();
    client.setForeground(true);
    expect(onForegroundRefresh).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchConfirmations).toHaveBeenCalledTimes(1);
  });
});

describe('退避序列（§5.1：2s → 4s → 8s，上限 15s）', () => {
  it('SSE_BACKOFF_STEPS 契约', () => {
    expect(SSE_BACKOFF_STEPS).toEqual([2000, 4000, 8000, 15000]);
  });
});
