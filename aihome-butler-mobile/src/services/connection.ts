/**
 * LAN/Cloud 双通道探测与切换器（MOBILE_APP_SPEC.md §2.1、§7 services/connection.ts）。
 *
 * 状态机（与服务端 /api/health 公开探针对接）：
 *   App 启动/切回前台 → 本地有 token？
 *     无 → 需要配对（offline + needsPairing）
 *     有 → 探测 lanUrl（1.5s 超时）→ 200 且 status=ok → LAN 直连
 *          失败 → 配置了 cloudUrl？→ 探测（3s 超时）→ 200 → Cloud 中转
 *                                    → 失败 → Offline（顶部告警 + 手动重试）
 */
import { TIMEOUTS, request } from './http';
import type { HealthResponse } from '@/types/api';

export type Channel = 'lan' | 'cloud' | 'offline';

export interface ProbeResult {
  ok: boolean;
  health?: HealthResponse;
  latencyMs?: number;
  error?: string;
}

/** 去掉末尾斜杠与空白，规范化 base URL。 */
export function normalizeBaseUrl(raw: string): string {
  return (raw || '').trim().replace(/\/+$/, '');
}

/** URL 形式合法性（http/https 开头）。 */
export function isPlausibleUrl(raw: string): boolean {
  return /^https?:\/\/.+/i.test(normalizeBaseUrl(raw));
}

/**
 * 探测候选地址 + /api/health（双通道探活与协议版本校验）。
 * 通过 baseUrl 显式指向候选地址，不影响当前已激活通道。
 */
export async function probeHealthAt(rawBase: string, timeoutMs: number): Promise<ProbeResult> {
  const normalized = normalizeBaseUrl(rawBase);
  if (!normalized) {
    return { ok: false, error: '地址为空' };
  }
  if (!isPlausibleUrl(normalized)) {
    return { ok: false, error: '地址需以 http:// 或 https:// 开头' };
  }
  const started = Date.now();
  try {
    const health = await request<HealthResponse>('/api/health', {
      method: 'GET',
      auth: false,
      timeout: timeoutMs,
      baseUrl: normalized,
    });
    return {
      ok: health.status === 'ok',
      health,
      latencyMs: Date.now() - started,
      error: health.status === 'ok' ? undefined : '服务状态异常',
    };
  } catch (exc) {
    const message = exc instanceof Error && exc.message ? exc.message : '连接失败';
    return { ok: false, latencyMs: Date.now() - started, error: message };
  }
}

export interface ChannelInput {
  hasToken: boolean;
  lanUrl: string;
  cloudUrl: string;
}

export interface ChannelDecision {
  channel: Channel;
  /** 实际用于请求的 base URL；offline 时为空。 */
  activeBaseUrl: string;
  reason: string;
}

/**
 * 双通道自动切换决策（纯函数，可单测）：
 * LAN 优先 → Cloud 兜底 → Offline。
 */
export function decideChannel(
  input: ChannelInput,
  lan: ProbeResult,
  cloud: ProbeResult | null,
): ChannelDecision {
  if (!input.hasToken) {
    return { channel: 'offline', activeBaseUrl: '', reason: '未配对' };
  }
  if (lan.ok) {
    return { channel: 'lan', activeBaseUrl: normalizeBaseUrl(input.lanUrl), reason: '局域网直连' };
  }
  if (normalizeBaseUrl(input.cloudUrl) && cloud && cloud.ok) {
    return { channel: 'cloud', activeBaseUrl: normalizeBaseUrl(input.cloudUrl), reason: '云中转' };
  }
  return { channel: 'offline', activeBaseUrl: '', reason: '局域网与云端均不可达' };
}

/** 完整探测决策：lan → cloud 依次探测（LAN 成功或云端未配置则跳过云端探测）。 */
export async function resolveChannel(input: ChannelInput): Promise<ChannelDecision> {
  if (!input.hasToken) {
    return { channel: 'offline', activeBaseUrl: '', reason: '未配对' };
  }
  const lan = await probeHealthAt(input.lanUrl, TIMEOUTS.HEALTH_LAN);
  let cloud: ProbeResult | null = null;
  if (!lan.ok && normalizeBaseUrl(input.cloudUrl)) {
    cloud = await probeHealthAt(input.cloudUrl, TIMEOUTS.HEALTH_CLOUD);
  }
  return decideChannel(input, lan, cloud);
}
