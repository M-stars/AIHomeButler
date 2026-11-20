/**
 * 测试公共工具：uni 运行时 mock。
 * http.ts / storage.ts 均通过 globalThis.uni 访问原生能力；
 * 测试注入一个可编程的假 uni，按注册的响应器回放请求结果。
 */
import { vi } from 'vitest';

export interface MockRequestCall {
  url: string;
  method: string;
  data?: any;
  header?: Record<string, string>;
  timeout?: number;
  responseType?: string;
}

export type MockResponder = (
  call: MockRequestCall,
) => { statusCode: number; data: any } | { errMsg: string };

export interface UniMock {
  calls: MockRequestCall[];
  setResponder(fn: MockResponder): void;
  storage: Map<string, string>;
}

export function installUniMock(): UniMock {
  const calls: MockRequestCall[] = [];
  const storage = new Map<string, string>();
  let responder: MockResponder = () => ({ statusCode: 404, data: {} });
  const uni = {
    request(opts: any) {
      calls.push({
        url: opts.url,
        method: opts.method,
        data: opts.data,
        header: opts.header,
        timeout: opts.timeout,
        responseType: opts.responseType,
      });
      setTimeout(() => {
        const res = responder({
          url: opts.url,
          method: opts.method,
          data: opts.data,
          header: opts.header,
          timeout: opts.timeout,
          responseType: opts.responseType,
        });
        if ('errMsg' in res) {
          opts.fail({ errMsg: res.errMsg });
        } else {
          opts.success({ statusCode: res.statusCode, data: res.data });
        }
      }, 0);
    },
    getStorageSync: (key: string) => storage.get(key) ?? '',
    setStorageSync: (key: string, value: string) => storage.set(key, String(value)),
    removeStorageSync: (key: string) => storage.delete(key),
    showToast: vi.fn(),
    showModal: vi.fn(),
    reLaunch: vi.fn(),
    vibrateShort: vi.fn(),
  };
  (globalThis as any).uni = uni;
  return { calls, setResponder: (fn: MockResponder) => (responder = fn), storage };
}

export function okJson(statusCode: number, data: any) {
  return { statusCode, data };
}

/** 清空 http 模块的连接配置（防止用例间串扰）。 */
export async function resetHttp(): Promise<void> {
  const { configureHttp, onUnauthorized } = await import('@/services/http');
  configureHttp('', '');
  onUnauthorized(null);
}

/** 建立 pinia 测试环境。 */
export async function setupPinia(): Promise<void> {
  const { createPinia, setActivePinia } = await import('pinia');
  setActivePinia(createPinia());
}
