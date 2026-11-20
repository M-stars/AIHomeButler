/**
 * 多端凭证与配置的安全存储封装（MOBILE_APP_SPEC.md §7 services/storage.ts）。
 *
 * 设计说明：
 *  - uni-app 各端底层已有持久化实现：H5 落 localStorage、App 落原生 storage、
 *    小程序落加密 storage；此处统一收敛为同步 KV 接口。
 *  - 若后续接入原生 Keystore / Keychain 插件（App 端加固），仅需替换本文件的
 *    底层实现，调用方（stores/auth、pairing 页）不受影响。
 *  - 令牌绝不打印明文日志；测试环境（无 uni 运行时）自动降级为内存 Map。
 */

export const STORAGE_KEYS = {
  TOKEN: 'bh_token',
  DEVICE_ID: 'bh_device_id',
  DEVICE_NAME: 'bh_device_name',
  LAN_URL: 'bh_lan_url',
  CLOUD_URL: 'bh_cloud_url',
  THEME: 'bh_theme',
} as const;

/** 内存兜底（仅单元测试环境使用；正常端上都有 uni 持久化实现）。 */
const memoryStore = new Map<string, string>();

function hasUniRuntime(): boolean {
  return typeof (globalThis as any).uni !== 'undefined';
}

export function getString(key: string): string {
  if (hasUniRuntime()) {
    try {
      const raw = (globalThis as any).uni.getStorageSync(key);
      return typeof raw === 'string' ? raw : '';
    } catch {
      return '';
    }
  }
  return memoryStore.get(key) ?? '';
}

export function setString(key: string, value: string): void {
  if (hasUniRuntime()) {
    try {
      (globalThis as any).uni.setStorageSync(key, value);
    } catch {
      /* 存储失败不中断主流程 */
    }
  } else {
    memoryStore.set(key, value);
  }
}

export function removeKey(key: string): void {
  if (hasUniRuntime()) {
    try {
      (globalThis as any).uni.removeStorageSync(key);
    } catch {
      /* 忽略 */
    }
  } else {
    memoryStore.delete(key);
  }
}

/** 退出登录：清除全部本地凭证（连接地址保留，方便重新配对）。 */
export function clearCredentials(): void {
  removeKey(STORAGE_KEYS.TOKEN);
  removeKey(STORAGE_KEYS.DEVICE_ID);
  removeKey(STORAGE_KEYS.DEVICE_NAME);
}

export function clearAll(): void {
  Object.values(STORAGE_KEYS).forEach(removeKey);
}
