/**
 * HTTP 封装测试：Bearer 注入 / 401 统一拦截 / 409 冲突 / 超时分级。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { installUniMock, okJson, resetHttp, type UniMock } from './helpers';
import {
  ApiConflictError,
  ApiError,
  configureHttp,
  getAuthToken,
  onUnauthorized,
  request,
  requestBinary,
  TIMEOUTS,
} from '@/services/http';
import * as butlerApi from '@/services/butlerApi';

let mock: UniMock;

beforeEach(async () => {
  mock = installUniMock();
  await resetHttp();
});

describe('request 基础行为', () => {
  it('注入 Authorization: Bearer 头', async () => {
    configureHttp('http://b', 'tok123');
    mock.setResponder(() => okJson(200, { ok: 1 }));
    await request('/api/system/info');
    expect(mock.calls[0].header?.['Authorization']).toBe('Bearer tok123');
  });

  it('auth:false 不携带令牌（公开路径）', async () => {
    configureHttp('http://b', 'tok123');
    mock.setResponder(() => okJson(200, { status: 'ok' }));
    await request('/api/health', { auth: false });
    expect(mock.calls[0].header?.['Authorization']).toBeUndefined();
  });

  it('默认超时 15s', async () => {
    configureHttp('http://b', '');
    mock.setResponder(() => okJson(200, {}));
    await request('/api/devices');
    expect(mock.calls[0].timeout).toBe(TIMEOUTS.NORMAL);
  });

  it('400 解析 detail 字段抛 ApiError', async () => {
    configureHttp('http://b', '');
    mock.setResponder(() => okJson(400, { detail: '设备不支持该动作' }));
    await expect(request('/x', { method: 'POST' })).rejects.toMatchObject({
      name: 'ApiError',
      status: 400,
      detail: '设备不支持该动作',
    });
  });

  it('网络失败抛 ApiNetworkError', async () => {
    configureHttp('http://b', '');
    mock.setResponder(() => ({ errMsg: 'request:fail timeout' }));
    await expect(request('/x')).rejects.toMatchObject({ name: 'ApiNetworkError' });
  });
});

describe('401 统一拦截（§2.3 令牌失效收敛）', () => {
  it('清除令牌并触发回调', async () => {
    configureHttp('http://b', 'dead-token');
    const handler = vi.fn();
    onUnauthorized(handler);
    mock.setResponder(() => okJson(401, { detail: '未授权' }));
    await expect(request('/api/overview')).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
    expect(getAuthToken()).toBe('');
  });
});

describe('409 冲突（对话/任务并发）', () => {
  it('抛 ApiConflictError 且 detail 可读', async () => {
    configureHttp('http://b', '');
    mock.setResponder(() => okJson(409, { detail: '已有对话在处理中，请稍候' }));
    await expect(request('/api/chat', { method: 'POST' })).rejects.toBeInstanceOf(ApiConflictError);
  });
});

describe('requestBinary（TTS WAV）', () => {
  it('arraybuffer 成功路径', async () => {
    configureHttp('http://b', 'tok');
    const bytes = new Uint8Array([1, 2, 3]).buffer;
    mock.setResponder(() => ({ statusCode: 200, data: bytes }));
    const out = await requestBinary('/api/tts', { method: 'POST' });
    expect(out).toBe(bytes);
    expect(mock.calls[0].responseType).toBe('arraybuffer');
  });

  it('失败时从 arraybuffer 解出 detail', async () => {
    configureHttp('http://b', 'tok');
    const text = new TextEncoder().encode(JSON.stringify({ detail: '语音模块未装配' })).buffer;
    mock.setResponder(() => ({ statusCode: 503, data: text }));
    await expect(requestBinary('/api/tts', { method: 'POST' })).rejects.toMatchObject({
      status: 503,
      detail: '语音模块未装配',
    });
  });
});

describe('超时分级（§5：对话/任务 ≥180s）', () => {
  it('chat 180000ms / task 180000ms / 直控 15000ms', async () => {
    configureHttp('http://b', 'tok');
    mock.setResponder(() => okJson(200, { reply: '', tool_events: [], notifications: [] }));
    await butlerApi.chat('你好');
    expect(mock.calls[0].timeout).toBe(180000);

    mock.setResponder(() => okJson(200, { status: 'completed', steps: [], summary: '' }));
    await butlerApi.runTask('任务');
    expect(mock.calls[1].timeout).toBe(180000);

    mock.setResponder(() => okJson(200, { status: 'ok', verified: true }));
    await butlerApi.sendDeviceCommand('light.a', 'turn_on');
    expect(mock.calls[2].timeout).toBe(TIMEOUTS.NORMAL);
  });
});

describe('butlerApi 直控请求体契约（无 value 省略键）', () => {
  it('turn_on 不带 value 字段', async () => {
    configureHttp('http://b', 'tok');
    mock.setResponder(() => okJson(200, { status: 'ok' }));
    await butlerApi.sendDeviceCommand('light.a', 'turn_on');
    expect(mock.calls[0].data).toEqual({ action: 'turn_on' });
  });

  it('set_brightness 带 value', async () => {
    configureHttp('http://b', 'tok');
    mock.setResponder(() => okJson(200, { status: 'ok' }));
    await butlerApi.sendDeviceCommand('light.a', 'set_brightness', 40);
    expect(mock.calls[0].data).toEqual({ action: 'set_brightness', value: 40 });
  });
});
