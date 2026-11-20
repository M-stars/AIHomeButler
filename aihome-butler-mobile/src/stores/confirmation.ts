/**
 * 全局待确认队列 store（MOBILE_APP_SPEC.md §3.1、§7 stores/confirmation.ts）。
 *
 * 状态机：
 *   轮询 /api/confirmations 或 SSE confirmation_pending → items 入队；
 *   用户点批准/拒绝（防重入：点击即置灰）→ POST /api/confirmations/{id}
 *   → SSE confirmation_resolved 或本地移除出队。
 *
 * 安全红线：绝不提供通知栏一键静默批准——审批必须打开 App 看清参数后点选。
 */
import { defineStore } from 'pinia';
import * as butlerApi from '@/services/butlerApi';
import type { ConfirmationItem } from '@/types/api';

export const useConfirmationStore = defineStore('confirmation', {
  state: () => ({
    items: [] as ConfirmationItem[],
    /** 正在提交应答的确认 id（防重入）。 */
    respondingIds: [] as string[],
    lastSyncedAt: 0,
  }),

  getters: {
    /** 待确认数（>0 时管家 Tab 图标显示琥珀角标并弹出审批抽屉）。 */
    count(state): number {
      return state.items.length;
    },
    isResponding(state) {
      return (id: string): boolean => state.respondingIds.includes(id);
    },
  },

  actions: {
    /** 轮询兜底拉取待确认队列。 */
    async load(): Promise<void> {
      try {
        const res = await butlerApi.getConfirmations();
        this.mergePending(res.confirmations);
        this.lastSyncedAt = Date.now();
      } catch {
        /* 离线/401 由 http 层统一收敛，这里不重复打扰 */
      }
    },

    /** 以服务端队列为准合并（移除已消失项，追加新项）。 */
    mergePending(pending: ConfirmationItem[]): void {
      const serverIds = new Set(pending.map((p) => p.id));
      this.items = this.items.filter((it) => serverIds.has(it.id));
      pending.forEach((p) => {
        if (!this.items.some((it) => it.id === p.id)) {
          this.items.push(p);
        }
      });
    },

    /** SSE confirmation_pending。 */
    onPending(item: ConfirmationItem): void {
      if (!this.items.some((it) => it.id === item.id)) {
        this.items.push(item);
      }
    },

    /** SSE confirmation_resolved：无论本端还是管家台应答，都出队。 */
    onResolved(payload: { id: string; approved: boolean; summary: string }): void {
      this.items = this.items.filter((it) => it.id !== payload.id);
      this.respondingIds = this.respondingIds.filter((id) => id !== payload.id);
    },

    /**
     * 审批应答。防重入：重复点击/已出队直接返回 false。
     * 应答成功后本地立即出队（不等 SSE，避免按钮停留）。
     */
    async respond(id: string, approved: boolean): Promise<boolean> {
      if (this.respondingIds.includes(id)) {
        return false;
      }
      if (!this.items.some((it) => it.id === id)) {
        return false;
      }
      this.respondingIds.push(id);
      try {
        await butlerApi.respondConfirmation(id, approved);
        this.onResolved({ id, approved, summary: '' });
        return true;
      } catch (exc) {
        // 404 = 已被处理（管家台先点了）：出队即可
        const status = (exc as { status?: number }).status;
        if (status === 404) {
          this.onResolved({ id, approved, summary: '' });
        }
        return false;
      } finally {
        this.respondingIds = this.respondingIds.filter((rid) => rid !== id);
      }
    },

    clear(): void {
      this.items = [];
      this.respondingIds = [];
    },
  },
});
