/**
 * 实时事件层（MOBILE_APP_SPEC.md §5.1、§7 services/realtime.ts）。
 *
 * 自适应策略（SSE + 分级短轮询双模传输）：
 *  - H5 / App 端：SSE 订阅 /api/events（H5 用 EventSource；App 用
 *    uni.request enableChunked 流式读取，失败自动降级为短轮询）；
 *  - 微信小程序端：不支持 SSE，统一 RealtimeAdapter 短轮询——
 *    对话/任务请求进行期间每 1s 轮询 /api/confirmations，空闲期每 4s
 *    轮询确认与设备状态；
 *  - SSE 断线指数退避重连 2s → 4s → 8s，上限 15s；
 *  - App 切后台主动断开省电，切回前台立刻重连并拉取一次
 *    /api/overview 与 /api/confirmations。
 */
import { getAuthToken, getBaseUrl, utf8Decode } from './http';
import { isWebEnv } from './environment';
import type { ConfirmationItem } from '@/types/api';

/** 小程序分级短轮询节奏。 */
export const POLL_ACTIVE_MS = 1000;
export const POLL_IDLE_MS = 4000;
/** 空闲期设备状态轮询节奏（每 2 个空闲 tick 一次，避免与确认轮询叠加过重）。 */
export const DEVICE_POLL_TICKS = 2;
/** SSE 断线指数退避（上限 15s）。 */
export const SSE_BACKOFF_STEPS = [2000, 4000, 8000, 15000];

export interface RealtimeHandlers {
  /** SSE 命名事件（connected/pipeline_step/confirmation_pending 等）。 */
  onEvent(name: string, data: any): void;
  /** 轮询确认队列结果（小程序模式 / SSE 降级时使用）。 */
  onConfirmations(items: ConfirmationItem[]): void;
  /** 连接建立/断开（用于顶部状态胶囊）。 */
  onConnectionChange?(up: boolean): void;
  /** 切回前台：立刻拉一次 overview 与 confirmations。 */
  onForegroundRefresh?(): void;
}

export interface RealtimeDeps {
  fetchConfirmations: () => Promise<{ confirmations: ConfirmationItem[] }>;
  /** 可选：空闲期设备状态轮询（小程序模式）。 */
  fetchDevices?: () => Promise<unknown>;
  /** 注入定时器便于单测（缺省 setTimeout/clearTimeout）。 */
  timers?: {
    setTimeout: typeof setTimeout;
    clearTimeout: typeof clearTimeout;
  };
}

/**
 * 解析 SSE 文本帧缓冲：按空行分帧，取 event:/data: 两行。
 * 返回 (事件名, 数据对象) 数组与剩余半帧。
 */
export function parseSseBuffer(
  buffer: string,
): { events: Array<{ name: string; data: any }>; rest: string } {
  const events: Array<{ name: string; data: any }> = [];
  const frames = buffer.split('\n\n');
  const rest = frames.pop() ?? '';
  for (const frame of frames) {
    let name = '';
    let dataText = '';
    for (const line of frame.split('\n')) {
      const trimmed = line.trim();
      if (trimmed.startsWith('event:')) {
        name = trimmed.slice(6).trim();
      } else if (trimmed.startsWith('data:')) {
        dataText += trimmed.slice(5).trim();
      }
    }
    if (!name || !dataText) continue;
    try {
      events.push({ name, data: JSON.parse(dataText) });
    } catch {
      // 心跳注释帧等非 JSON 数据：跳过
    }
  }
  return { events, rest };
}

export class RealtimeClient {
  private handlers: RealtimeHandlers | null = null;
  private deps: RealtimeDeps;
  private es: any = null;
  private requestTask: any = null;
  private pollTimer: any = null;
  private reconnectTimer: any = null;
  private backoffIdx = 0;
  private idleTicks = 0;
  private running = false;
  private foreground = true;
  private inFlight = false;
  private sseUp = false;
  /** App 端 chunked SSE 失败后自动降级为轮询。 */
  private chunkedFailed = false;

  constructor(deps: RealtimeDeps) {
    this.deps = deps;
  }

  start(handlers: RealtimeHandlers): void {
    this.handlers = handlers;
    this.running = true;
    this.connect();
  }

  stop(): void {
    this.running = false;
    this.closeStreams();
    this.clearTimers();
    this.setSseUp(false);
  }

  /** 对话/任务请求进行中：轮询节奏提到 1s（§5.1）。 */
  setActive(active: boolean): void {
    this.inFlight = active;
    if (this.running && !this.isSseActive()) {
      // 打断已排定的慢速轮询，立刻按活跃节奏重新调度
      const t = this.deps.timers ?? { setTimeout, clearTimeout };
      if (this.pollTimer) {
        t.clearTimeout(this.pollTimer);
        this.pollTimer = null;
      }
      this.schedulePoll(0);
    }
  }

  /** App 切后台 true/false：后台断开省电，回前台立刻重连+刷新。 */
  setForeground(fg: boolean): void {
    if (fg === this.foreground) return;
    this.foreground = fg;
    if (!fg) {
      this.closeStreams();
      this.clearTimers();
    } else if (this.running) {
      this.connect();
      if (this.handlers?.onForegroundRefresh) {
        this.handlers.onForegroundRefresh();
      }
    }
  }

  isSseActive(): boolean {
    return this.sseUp && !this.chunkedFailed;
  }

  private setSseUp(up: boolean): void {
    if (this.sseUp !== up) {
      this.sseUp = up;
      this.handlers?.onConnectionChange?.(up);
      if (!up) {
        // SSE 断开期间用短轮询兜底（确认请求不能漏）
        this.schedulePoll(0);
      }
    }
  }

  private clearTimers(): void {
    const t = this.deps.timers ?? {
      setTimeout,
      clearTimeout,
    };
    if (this.pollTimer) {
      t.clearTimeout(this.pollTimer);
      this.pollTimer = null;
    }
    if (this.reconnectTimer) {
      t.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private closeStreams(): void {
    if (this.es) {
      try {
        this.es.close();
      } catch {
        /* 忽略 */
      }
      this.es = null;
    }
    if (this.requestTask) {
      try {
        this.requestTask.abort();
      } catch {
        /* 忽略 */
      }
      this.requestTask = null;
    }
  }

  private scheduleReconnect(): void {
    if (!this.running || !this.foreground) return;
    const t = this.deps.timers ?? { setTimeout, clearTimeout };
    const delay = SSE_BACKOFF_STEPS[Math.min(this.backoffIdx, SSE_BACKOFF_STEPS.length - 1)];
    this.backoffIdx += 1;
    this.reconnectTimer = t.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private connect(): void {
    if (!this.running || !this.foreground) return;
    this.clearTimers();
    const base = getBaseUrl();
    const token = getAuthToken();
    if (!base || !token) {
      // 未连接/未配对：退回纯轮询（轮询会因无 token 静默失败）
      this.schedulePoll(0);
      return;
    }
    if (isWebEnv() && typeof (globalThis as any).EventSource === 'function') {
      this.connectEventSource(base, token);
    } else {
      this.connectChunkedSse(base, token);
    }
  }

  /** H5：原生 EventSource（token 走 ?token= 查询参数）。 */
  private connectEventSource(base: string, token: string): void {
    const url = `${base}/api/events?token=${encodeURIComponent(token)}`;
    const es = new (globalThis as any).EventSource(url);
    this.es = es;
    es.onopen = () => {
      this.backoffIdx = 0;
      this.setSseUp(true);
    };
    es.onmessage = (ev: MessageEvent) => {
      // 未命名事件（服务端只有命名事件）：忽略
      this.dispatchSse('', ev.data);
    };
    const named = [
      'connected',
      'pipeline_step',
      'confirmation_pending',
      'confirmation_resolved',
      'device_updated',
      'notification',
    ];
    named.forEach((name) => {
      es.addEventListener(name, (ev: MessageEvent) => this.dispatchSse(name, ev.data));
    });
    es.onerror = () => {
      this.setSseUp(false);
      this.closeStreams();
      this.scheduleReconnect();
    };
  }

  /**
   * App 端：uni.request enableChunked 流式读取 SSE。
   * 任何失败（不支持 chunked / 连接失败）自动降级为分级短轮询。
   */
  private connectChunkedSse(base: string, token: string): void {
    const uniApi = (globalThis as any).uni;
    if (!uniApi || typeof uniApi.request !== 'function') {
      this.chunkedFailed = true;
      this.schedulePoll(0);
      return;
    }
    let buffer = '';
    let gotChunk = false;
    let task: any = null;
    try {
      task = uniApi.request({
        url: `${base}/api/events?token=${encodeURIComponent(token)}`,
        method: 'GET',
        header: { Accept: 'text/event-stream' },
        enableChunked: true,
        responseType: 'arraybuffer',
        success: () => {
          // 连接关闭：退避重连
          this.setSseUp(false);
          this.scheduleReconnect();
        },
        fail: () => {
          this.setSseUp(false);
          if (!gotChunk) {
            // 一次分片都没收到：认定环境不支持，降级轮询
            this.chunkedFailed = true;
            this.schedulePoll(0);
          } else {
            this.scheduleReconnect();
          }
        },
      });
    } catch {
      this.chunkedFailed = true;
      this.schedulePoll(0);
      return;
    }
    this.requestTask = task;
    if (task && typeof task.onChunkReceived === 'function') {
      task.onChunkReceived((res: { data: ArrayBuffer | Uint8Array }) => {
        gotChunk = true;
        this.backoffIdx = 0;
        this.setSseUp(true);
        const bytes = res.data instanceof Uint8Array ? res.data : new Uint8Array(res.data || []);
        buffer += utf8Decode(bytes);
        const parsed = parseSseBuffer(buffer);
        buffer = parsed.rest;
        parsed.events.forEach((e) => this.handlers?.onEvent(e.name, e.data));
      });
    } else {
      // RequestTask 不支持 onChunkReceived：降级轮询
      this.chunkedFailed = true;
      this.closeStreams();
      this.schedulePoll(0);
    }
  }

  private dispatchSse(name: string, dataText: string): void {
    let data: any = null;
    try {
      data = dataText ? JSON.parse(dataText) : null;
    } catch {
      return;
    }
    if (name === 'connected') {
      this.backoffIdx = 0;
      this.setSseUp(true);
    }
    this.handlers?.onEvent(name, data);
  }

  private schedulePoll(delay?: number): void {
    if (!this.running || !this.foreground) return;
    if (this.pollTimer || this.isSseActive()) return;
    const t = this.deps.timers ?? { setTimeout, clearTimeout };
    const interval = this.inFlight ? POLL_ACTIVE_MS : POLL_IDLE_MS;
    this.pollTimer = t.setTimeout(async () => {
      this.pollTimer = null;
      await this.pollOnce();
      this.schedulePoll();
    }, delay !== undefined ? delay : interval);
  }

  /** 单次轮询：确认队列必查；空闲期按节奏捎带设备状态（小程序模式）。 */
  async pollOnce(): Promise<void> {
    if (!this.running) return;
    try {
      const res = await this.deps.fetchConfirmations();
      this.handlers?.onConfirmations(res.confirmations);
    } catch {
      /* 失败由下一轮退避兜底 */
      return;
    }
    if (this.deps.fetchDevices) {
      this.idleTicks += 1;
      if (this.idleTicks % DEVICE_POLL_TICKS === 0) {
        try {
          await this.deps.fetchDevices();
        } catch {
          /* 设备轮询失败不打扰 */
        }
      }
    }
  }
}
