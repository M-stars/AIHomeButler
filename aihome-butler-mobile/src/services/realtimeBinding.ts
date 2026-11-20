/**
 * 实时层与 Pinia store 的组合根（依赖注入装配点）。
 *
 * RealtimeClient 本身零 store 依赖（可单测）；此处把它接到
 * confirmation / devices / chat 三个 store，并注入轮询数据源。
 */
import { RealtimeClient } from './realtime';
import * as butlerApi from './butlerApi';
import { useAuthStore } from '@/stores/auth';
import { useConfirmationStore } from '@/stores/confirmation';
import { useDevicesStore } from '@/stores/devices';
import { useChatStore } from '@/stores/chat';

let client: RealtimeClient | null = null;

export function getRealtimeClient(): RealtimeClient {
  if (!client) {
    client = new RealtimeClient({
      fetchConfirmations: () => butlerApi.getConfirmations(),
      fetchDevices: () => useDevicesStore().refresh(),
    });
  }
  return client;
}

/** 启动实时层（App.vue onLaunch 在 bootstrap 成功后调用）。 */
export function startRealtime(): void {
  const auth = useAuthStore();
  const realtime = getRealtimeClient();
  realtime.start({
    onEvent: (name: string, data: any) => {
      switch (name) {
        case 'pipeline_step':
          useChatStore().onPipelineEvent(data);
          break;
        case 'confirmation_pending':
          useConfirmationStore().onPending(data);
          break;
        case 'confirmation_resolved':
          useConfirmationStore().onResolved(data);
          break;
        case 'device_updated':
          useDevicesStore().applyDeviceUpdated(data);
          break;
        default:
          // 'connected' / 'notification'（服务端当前未发布 notification，
          // 预留兼容未来版本；通知走 /api/notifications 轮询）
          break;
      }
    },
    onConfirmations: (items) => useConfirmationStore().mergePending(items),
    onConnectionChange: (up: boolean) => {
      // 连接态只影响实时层自身节奏；通道胶囊以 auth.channel 为准
      if (up) {
        auth.refreshSystemInfo();
      }
    },
    onForegroundRefresh: () => {
      // 切回前台：立刻拉一次 overview 与 confirmations（§5.1）
      useDevicesStore().refresh();
      useConfirmationStore().load();
      auth.refreshSystemInfo();
    },
  });
}

export function stopRealtime(): void {
  getRealtimeClient().stop();
}

/** App 切后台/回前台。 */
export function setRealtimeForeground(fg: boolean): void {
  getRealtimeClient().setForeground(fg);
}

/**
 * 对话/任务请求期间的活跃标记：轮询模式节奏提到 1s
 * （SSE 模式下确认走事件推送，无需提速）。
 */
export function withActiveRealtime<T>(promise: Promise<T>): Promise<T> {
  const realtime = getRealtimeClient();
  realtime.setActive(true);
  return promise.finally(() => realtime.setActive(false));
}
