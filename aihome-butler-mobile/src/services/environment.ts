/**
 * 平台环境工具：运行平台探测、触觉反馈、TTS WAV 播放。
 * 全部做运行时守卫：任何能力缺失只降级、不抛错（语音/震动从不打断主流程）。
 */

export type AppPlatform = 'android' | 'ios' | 'miniprogram' | 'web' | 'pc';

/** 是否浏览器环境（H5 端，支持 EventSource/Blob）。 */
export function isWebEnv(): boolean {
  return typeof (globalThis as any).window !== 'undefined'
    && typeof (globalThis as any).document !== 'undefined';
}

/** 是否小程序环境（不支持 SSE/EventSource，实时层自动降级短轮询）。 */
export function isMiniProgramEnv(): boolean {
  if (isWebEnv()) return false;
  try {
    const info = (globalThis as any).uni?.getSystemInfoSync?.();
    const up = String(info?.uniPlatform || '');
    return up.startsWith('mp');
  } catch {
    return false;
  }
}

/**
 * 配对接口 platform 字段取值（契约：android/ios/miniprogram/pc/web）。
 * H5 → web；微信等小程序 → miniprogram；App 按系统归 android/ios。
 */
export function detectPlatform(): AppPlatform {
  if (isWebEnv()) return 'web';
  try {
    const info = (globalThis as any).uni?.getSystemInfoSync?.();
    const uniPlatform = String(info?.uniPlatform || '');
    if (uniPlatform.startsWith('mp')) return 'miniprogram';
    const p = String(info?.platform || '');
    if (p === 'ios') return 'ios';
    if (p === 'android' || uniPlatform === 'app') return 'android';
    if (uniPlatform === 'cli' || p === 'windows' || p === 'mac') return 'pc';
  } catch {
    /* 缺省 web */
  }
  return 'web';
}

/** 轻触觉反馈（switch 开关点击；H5 无震动硬件时静默忽略）。 */
export function hapticFeedback(): void {
  try {
    (globalThis as any).uni?.vibrateShort?.({ fail: () => undefined });
  } catch {
    /* 平台不支持则忽略 */
  }
}

/**
 * 播放 TTS 返回的 WAV 字节。
 * H5：Blob URL 直接喂 InnerAudioContext；
 * App/小程序：先写临时文件（uni.env.USER_DATA_PATH）再播放。
 * 失败仅 reject 由调用方 toast，不崩溃。
 */
export function playWavBytes(bytes: ArrayBuffer): Promise<void> {
  return new Promise((resolve, reject) => {
    try {
      const uniApi = (globalThis as any).uni;
      const audio = uniApi.createInnerAudioContext();
      if (isWebEnv() && typeof (globalThis as any).Blob === 'function') {
        const blob = new Blob([bytes], { type: 'audio/wav' });
        audio.src = URL.createObjectURL(blob);
        audio.play();
        audio.onEnded(() => {
          URL.revokeObjectURL(audio.src);
          resolve();
        });
        audio.onError(() => {
          URL.revokeObjectURL(audio.src);
          reject(new Error('音频播放失败'));
        });
        return;
      }
      const fs = uniApi.getFileSystemManager?.();
      const basePath = String(uniApi.env?.USER_DATA_PATH || '');
      if (!fs || !basePath) {
        reject(new Error('当前平台不支持音频播放'));
        return;
      }
      const filePath = `${basePath}/tts_${Date.now()}.wav`;
      fs.writeFile({
        filePath,
        data: bytes,
        success: () => {
          audio.src = filePath;
          audio.play();
          audio.onEnded(() => {
            try {
              fs.unlinkSync(filePath);
            } catch {
              /* 清理失败忽略 */
            }
            resolve();
          });
          audio.onError(() => reject(new Error('音频播放失败')));
        },
        fail: () => reject(new Error('临时音频文件写入失败')),
      });
    } catch (exc) {
      reject(exc instanceof Error ? exc : new Error('音频播放失败'));
    }
  });
}
