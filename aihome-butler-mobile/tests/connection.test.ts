/**
 * 双通道切换状态机测试（MOBILE_APP_SPEC.md §2.1 / 验收清单）。
 * 覆盖：LAN 优先 → Cloud 兜底 → Offline；探测超时分级 1.5s/3s。
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { installUniMock, okJson, resetHttp, type UniMock } from './helpers';
import { configureHttp } from '@/services/http';
import {
  decideChannel,
  normalizeBaseUrl,
  probeHealthAt,
  resolveChannel,
} from '@/services/connection';

let mock: UniMock;

beforeEach(async () => {
  mock = installUniMock();
  await resetHttp();
});

describe('normalizeBaseUrl', () => {
  it('去掉末尾斜杠与空白', () => {
    expect(normalizeBaseUrl('  http://a:8080/// ')).toBe('http://a:8080');
  });
});

describe('probeHealthAt', () => {
  it('非法地址直接拒绝，不发请求', async () => {
    const r1 = await probeHealthAt('', 1500);
    expect(r1.ok).toBe(false);
    const r2 = await probeHealthAt('192.168.1.5:8080', 1500);
    expect(r2.ok).toBe(false);
    expect(mock.calls.length).toBe(0);
  });

  it('status=ok 视为可用并记录延迟', async () => {
    configureHttp('http://placeholder', '');
    mock.setResponder(() => okJson(200, {
      status: 'ok', service: 'ai-home-butler', version: '2.0',
      protocol_version: '1.0', uptime_seconds: 5,
    }));
    const r = await probeHealthAt('http://lan:8080', 1500);
    expect(r.ok).toBe(true);
    expect(r.health?.protocol_version).toBe('1.0');
    expect(mock.calls[0].url).toBe('http://lan:8080/api/health');
    expect(mock.calls[0].timeout).toBe(1500);
  });
});

describe('decideChannel（纯函数状态机）', () => {
  const lanOk = { ok: true };
  const lanFail = { ok: false, error: '超时' };

  it('无 token → offline（需配对）', () => {
    const d = decideChannel(
      { hasToken: false, lanUrl: 'http://a', cloudUrl: 'http://b' },
      lanOk,
      null,
    );
    expect(d.channel).toBe('offline');
    expect(d.activeBaseUrl).toBe('');
  });

  it('LAN 可用 → lan 直连', () => {
    const d = decideChannel(
      { hasToken: true, lanUrl: 'http://a:8080/', cloudUrl: '' },
      lanOk,
      null,
    );
    expect(d.channel).toBe('lan');
    expect(d.activeBaseUrl).toBe('http://a:8080');
  });

  it('LAN 失败 + Cloud 可用 → cloud', () => {
    const d = decideChannel(
      { hasToken: true, lanUrl: 'http://a', cloudUrl: 'http://b' },
      lanFail,
      { ok: true },
    );
    expect(d.channel).toBe('cloud');
    expect(d.activeBaseUrl).toBe('http://b');
  });

  it('双失败 → offline', () => {
    const d = decideChannel(
      { hasToken: true, lanUrl: 'http://a', cloudUrl: 'http://b' },
      lanFail,
      { ok: false },
    );
    expect(d.channel).toBe('offline');
  });

  it('LAN 失败且未配置云地址 → offline（不探测）', () => {
    const d = decideChannel({ hasToken: true, lanUrl: 'http://a', cloudUrl: '' }, lanFail, null);
    expect(d.channel).toBe('offline');
  });
});

describe('resolveChannel（探测编排）', () => {
  it('LAN 成功时不探测云端', async () => {
    configureHttp('http://placeholder', 'tok');
    mock.setResponder(() => okJson(200, { status: 'ok', protocol_version: '1.0' }));
    const d = await resolveChannel({
      hasToken: true,
      lanUrl: 'http://lan:8080',
      cloudUrl: 'http://cloud',
    });
    expect(d.channel).toBe('lan');
    expect(mock.calls.length).toBe(1);
    expect(mock.calls[0].timeout).toBe(1500);
  });

  it('LAN 失败 → 云端兜底，超时 3s', async () => {
    configureHttp('http://placeholder', 'tok');
    mock.setResponder((call) => {
      if (call.url.startsWith('http://lan')) return { errMsg: 'request:fail timeout' };
      return okJson(200, { status: 'ok', protocol_version: '1.0' });
    });
    const d = await resolveChannel({
      hasToken: true,
      lanUrl: 'http://lan:8080',
      cloudUrl: 'http://cloud',
    });
    expect(d.channel).toBe('cloud');
    expect(d.activeBaseUrl).toBe('http://cloud');
    expect(mock.calls.length).toBe(2);
    expect(mock.calls[0].timeout).toBe(1500);
    expect(mock.calls[1].timeout).toBe(3000);
  });

  it('LAN 失败且云端也失败 → offline', async () => {
    configureHttp('http://placeholder', 'tok');
    mock.setResponder(() => ({ errMsg: 'request:fail' }));
    const d = await resolveChannel({
      hasToken: true,
      lanUrl: 'http://lan:8080',
      cloudUrl: 'http://cloud',
    });
    expect(d.channel).toBe('offline');
    expect(d.reason).toContain('不可达');
  });

  it('无 token 直接 offline，不发探测请求', async () => {
    const d = await resolveChannel({
      hasToken: false,
      lanUrl: 'http://lan:8080',
      cloudUrl: '',
    });
    expect(d.channel).toBe('offline');
    expect(mock.calls.length).toBe(0);
  });
});
