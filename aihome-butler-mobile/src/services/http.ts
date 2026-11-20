/**
 * 统一 HTTP 请求封装（MOBILE_APP_SPEC.md §7 services/http.ts）。
 *
 * 安全与容错设计：
 *  - 受保护接口统一注入 `Authorization: Bearer <token>`（服务端兼容
 *    X-Butler-Token 与 ?token=，本端固定用 Bearer，杜绝令牌进 URL）；
 *  - 超时分级（§5）：健康探测 1.5s(LAN)/3s(Cloud)、普通请求与设备直控 15s、
 *    对话与多步任务至少 180s（确认队列服务端最长等 120s，设短了会在用户
 *    还没点批准前断开请求）；
 *  - 401 统一拦截：清除本地凭证并广播 unauthorized 事件，由 auth store
 *    停止实时流并跳回配对页（§2.3）；
 *  - 409 冲突抛专用错误类型，对话页据此提示"管家正在处理上一条请求"。
 */

/** 超时预算（毫秒）。 */
export const TIMEOUTS = {
  HEALTH_LAN: 1500,
  HEALTH_CLOUD: 3000,
  NORMAL: 15000,
  CHAT: 180000,
  TASK: 180000,
} as const;

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  constructor(status: number, detail: string) {
    super(detail || `请求失败（HTTP ${status}）`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail || `请求失败（HTTP ${status}）`;
  }
}

/** 网络层失败（超时 / 断网 / DNS）。 */
export class ApiNetworkError extends Error {
  constructor(message = '网络连接失败，请检查管家服务是否可达') {
    super(message);
    this.name = 'ApiNetworkError';
  }
}

/** 409：服务端已有对话/任务在处理中。 */
export class ApiConflictError extends ApiError {
  constructor(detail: string) {
    super(409, detail);
    this.name = 'ApiConflictError';
  }
}

type UnauthorizedHandler = () => void;
let unauthorizedHandler: UnauthorizedHandler | null = null;

/** auth store 启动时注册：任何 401 触发该回调。 */
export function onUnauthorized(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler;
}

// ---------------- 当前连接配置（由 connection/auth 层写入） ----------------

let baseUrl = '';
let authToken = '';

export function configureHttp(base: string, token: string): void {
  baseUrl = (base || '').replace(/\/+$/, '');
  authToken = token || '';
}

export function getBaseUrl(): string {
  return baseUrl;
}

export function getAuthToken(): string {
  return authToken;
}

interface UniRequestOptions {
  url: string;
  method: string;
  data?: unknown;
  header?: Record<string, string>;
  timeout?: number;
  /** requestBinary 传 'arraybuffer'。 */
  responseType?: string;
}

interface UniResponseLike {
  statusCode: number;
  data: any;
}

function uniRequest(options: UniRequestOptions): Promise<UniResponseLike> {
  const uniApi = (globalThis as any).uni;
  if (!uniApi || typeof uniApi.request !== 'function') {
    return Promise.reject(new ApiNetworkError('uni 运行时不可用'));
  }
  return new Promise((resolve, reject) => {
    uniApi.request({
      ...options,
      success: (res: UniResponseLike) => resolve(res),
      fail: (err: { errMsg?: string }) => {
        const msg = err && err.errMsg ? err.errMsg : '';
        reject(new ApiNetworkError(msg.includes('timeout') ? '请求超时' : undefined));
      },
    });
  });
}

/**
 * 二进制请求（如 /api/tts 返回 audio/wav）：responseType=arraybuffer，
 * 错误收敛与 request 一致。失败抛 ApiError / ApiNetworkError。
 */
export async function requestBinary(path: string, options: RequestOptions = {}): Promise<ArrayBuffer> {
  const header: Record<string, string> = { 'Content-Type': 'application/json' };
  if (options.auth !== false && authToken) {
    header['Authorization'] = `Bearer ${authToken}`;
  }
  const targetBase = options.baseUrl !== undefined
    ? options.baseUrl.replace(/\/+$/, '')
    : baseUrl;
  let res: UniResponseLike;
  try {
    res = await uniRequest({
      url: `${targetBase}${path}`,
      method: options.method || 'GET',
      data: options.data as any,
      header,
      timeout: options.timeout ?? TIMEOUTS.NORMAL,
      responseType: 'arraybuffer',
    });
  } catch (exc) {
    if (exc instanceof ApiNetworkError) throw exc;
    throw new ApiNetworkError();
  }
  if (res.statusCode === 401) {
    authToken = '';
    if (unauthorizedHandler) unauthorizedHandler();
    throw new ApiError(401, '设备凭证已失效或被管理员解绑，请重新配对');
  }
  if (res.statusCode >= 200 && res.statusCode < 300) {
    return res.data as ArrayBuffer;
  }
  // 失败响应体可能是 arraybuffer：按 UTF-8 解出并尝试解析 {detail}
  let detail = '';
  if (res.data instanceof ArrayBuffer) {
    try {
      detail = extractDetail(safeParseJson(utf8Decode(new Uint8Array(res.data))));
    } catch {
      detail = '';
    }
  } else {
    detail = extractDetail(res.data);
  }
  if (res.statusCode === 409) {
    throw new ApiConflictError(detail || '已有对话在处理中，请稍候');
  }
  throw new ApiError(res.statusCode, detail);
}

/** 安全 JSON 解析（失败返回原文）。 */
function safeParseJson(text: string): any {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/** 极简 UTF-8 解码（无 TextDecoder 环境兜底）。 */
export function utf8Decode(bytes: Uint8Array): string {
  if (typeof (globalThis as any).TextDecoder === 'function') {
    try {
      return new (globalThis as any).TextDecoder('utf-8').decode(bytes);
    } catch {
      /* 降级到手动解码 */
    }
  }
  let out = '';
  let i = 0;
  while (i < bytes.length) {
    const b = bytes[i];
    if (b < 0x80) {
      out += String.fromCharCode(b);
      i += 1;
    } else if (b < 0xe0) {
      out += String.fromCharCode(((b & 0x1f) << 6) | (bytes[i + 1] & 0x3f));
      i += 2;
    } else if (b < 0xf0) {
      out += String.fromCharCode(
        ((b & 0x0f) << 12) | ((bytes[i + 1] & 0x3f) << 6) | (bytes[i + 2] & 0x3f),
      );
      i += 3;
    } else {
      const cp = ((b & 0x07) << 18) | ((bytes[i + 1] & 0x3f) << 12)
        | ((bytes[i + 2] & 0x3f) << 6) | (bytes[i + 3] & 0x3f);
      const adjusted = cp - 0x10000;
      out += String.fromCharCode(0xd800 + (adjusted >> 10), 0xdc00 + (adjusted & 0x3ff));
      i += 4;
    }
  }
  return out;
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  data?: unknown;
  /** 缺省 15s；对话/任务须传 TIMEOUTS.CHAT / TIMEOUTS.TASK。 */
  timeout?: number;
  /** 是否携带 Bearer 令牌（/api/health、/api/pair/confirm 为公开路径）。 */
  auth?: boolean;
  /**
   * 显式指定目标 base URL（双通道探测候选地址时使用）；
   * 缺省走 configureHttp 配置的当前通道地址。
   */
  baseUrl?: string;
}

/**
 * 发起请求并做统一错误收敛。返回 2xx 的响应体。
 * 注意：网络失败抛 ApiNetworkError，非 2xx 抛 ApiError（409 为子类）。
 */
export async function request<T = any>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method || 'GET';
  const header: Record<string, string> = { 'Content-Type': 'application/json' };
  if (options.auth !== false && authToken) {
    header['Authorization'] = `Bearer ${authToken}`;
  }
  const targetBase = options.baseUrl !== undefined
    ? options.baseUrl.replace(/\/+$/, '')
    : baseUrl;
  let res: UniResponseLike;
  try {
    res = await uniRequest({
      url: `${targetBase}${path}`,
      method,
      data: options.data as any,
      header,
      timeout: options.timeout ?? TIMEOUTS.NORMAL,
    });
  } catch (exc) {
    if (exc instanceof ApiNetworkError) throw exc;
    throw new ApiNetworkError();
  }

  if (res.statusCode === 401) {
    // 令牌失效或被踢除：清凭证 + 广播（§2.3），由 auth store 收敛跳转
    authToken = '';
    if (unauthorizedHandler) unauthorizedHandler();
    throw new ApiError(401, '设备凭证已失效或被管理员解绑，请重新配对');
  }
  if (res.statusCode >= 200 && res.statusCode < 300) {
    return res.data as T;
  }

  const detail = extractDetail(res.data);
  if (res.statusCode === 409) {
    throw new ApiConflictError(detail || '已有对话在处理中，请稍候');
  }
  throw new ApiError(res.statusCode, detail);
}

function extractDetail(data: any): string {
  if (data && typeof data === 'object' && typeof data.detail === 'string') {
    return data.detail;
  }
  if (typeof data === 'string' && data) {
    return data;
  }
  return '';
}
