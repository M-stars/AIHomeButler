/**
 * 全局确认审批状态机测试（MOBILE_APP_SPEC.md §3.1 / 验收清单第 6 项）。
 * 覆盖：轮询合并、SSE 入队、防重入、SSE/本地出队、404 兜底。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { installUniMock, resetHttp, setupPinia, type UniMock } from './helpers';

vi.mock('@/services/butlerApi', () => ({
  getConfirmations: vi.fn(),
  respondConfirmation: vi.fn(),
}));

import * as butlerApi from '@/services/butlerApi';
import { useConfirmationStore } from '@/stores/confirmation';
import type { ConfirmationItem } from '@/types/api';

let mock: UniMock;

function item(id: string, summary = '控制设备 light.desk_lamp：turn_on'): ConfirmationItem {
  return {
    id,
    summary,
    arguments: { device_id: 'light.desk_lamp', action: 'turn_on' },
    created_at: '2026-11-21T10:00:00+08:00',
    approved: false,
  };
}

beforeEach(async () => {
  mock = installUniMock();
  await resetHttp();
  await setupPinia();
  vi.clearAllMocks();
});

describe('队列合并（轮询为准）', () => {
  it('mergePending：追加新项并移除已消失项', () => {
    const store = useConfirmationStore();
    store.onPending(item('a'));
    store.onPending(item('b'));
    expect(store.count).toBe(2);
    // 服务端此时只剩 a
    store.mergePending([item('a')]);
    expect(store.count).toBe(1);
    expect(store.items[0].id).toBe('a');
  });

  it('onPending 幂等（不重复入队）', () => {
    const store = useConfirmationStore();
    store.onPending(item('a'));
    store.onPending(item('a'));
    expect(store.count).toBe(1);
  });
});

describe('审批应答（防重入 + 出队）', () => {
  it('批准成功 → 立即出队', async () => {
    const store = useConfirmationStore();
    store.onPending(item('a'));
    vi.mocked(butlerApi.respondConfirmation).mockResolvedValue({ resolved: true, approved: true });
    const ok = await store.respond('a', true);
    expect(ok).toBe(true);
    expect(butlerApi.respondConfirmation).toHaveBeenCalledWith('a', true);
    expect(store.count).toBe(0);
  });

  it('进行中重复点击返回 false（点击置灰防重入）', async () => {
    const store = useConfirmationStore();
    store.onPending(item('a'));
    let resolveApi!: (v: unknown) => void;
    vi.mocked(butlerApi.respondConfirmation).mockImplementation(
      () => new Promise((resolve) => (resolveApi = resolve)),
    );
    const first = store.respond('a', true);
    const second = await store.respond('a', true);
    expect(second).toBe(false);
    resolveApi({ resolved: true, approved: true });
    expect(await first).toBe(true);
    expect(butlerApi.respondConfirmation).toHaveBeenCalledTimes(1);
  });

  it('404（管家台先处理）→ 本地出队兜底', async () => {
    const store = useConfirmationStore();
    store.onPending(item('a'));
    vi.mocked(butlerApi.respondConfirmation).mockRejectedValue({ status: 404 });
    const ok = await store.respond('a', false);
    expect(ok).toBe(false);
    expect(store.count).toBe(0);
  });

  it('SSE confirmation_resolved → 出队（含管家台侧应答）', () => {
    const store = useConfirmationStore();
    store.onPending(item('a'));
    store.onResolved({ id: 'a', approved: true, summary: '' });
    expect(store.count).toBe(0);
  });
});
