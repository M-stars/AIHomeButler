/**
 * 对话/多步任务与 8 步安全流水线 store（MOBILE_APP_SPEC.md §3.3、§7 stores/chat.ts）。
 *
 * 流水线固定 8 节点（数据源自 SSE pipeline_step，对照 executor._emit）：
 *   register_check → json_parse → schema_validate → risk_check
 *   → dedup_check → user_confirm → execute → verify_audit
 * trace_id 变化时节点整体重置；节点状态着色 ok=绿/pending=蓝脉冲/
 * warn=琥珀/error=红/skipped=灰。
 */
import { defineStore } from 'pinia';
import * as butlerApi from '@/services/butlerApi';
import { ApiConflictError } from '@/services/http';
import type { PipelineStepEvent, PipelineStepName, TaskReport, ToolEvent } from '@/types/api';

export const PIPELINE_STEPS: Array<{ step: PipelineStepName; label: string }> = [
  { step: 'register_check', label: '白名单' },
  { step: 'json_parse', label: 'JSON解析' },
  { step: 'schema_validate', label: 'Schema校验' },
  { step: 'risk_check', label: '风险拦截' },
  { step: 'dedup_check', label: '3s防重放' },
  { step: 'user_confirm', label: '用户确认' },
  { step: 'execute', label: '超时执行' },
  { step: 'verify_audit', label: '回读与审计' },
];

export type PipelineNodeStatus = 'ok' | 'warn' | 'error' | 'pending' | 'skipped' | 'idle';

export interface PipelineNode {
  step: PipelineStepName;
  label: string;
  status: PipelineNodeStatus;
  detail: string;
  tool: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'butler';
  kind: 'text' | 'task-report';
  text: string;
  toolEvents: ToolEvent[];
  taskReport?: TaskReport;
  createdAt: number;
}

/** 产出一条新流水线节点序列（全部 idle）。 */
export function freshPipelineNodes(): PipelineNode[] {
  return PIPELINE_STEPS.map((s) => ({
    step: s.step,
    label: s.label,
    status: 'idle' as PipelineNodeStatus,
    detail: '',
    tool: '',
  }));
}

function makeId(): string {
  return `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

export const useChatStore = defineStore('chat', {
  state: () => ({
    mode: 'chat' as 'chat' | 'task',
    messages: [] as ChatMessage[],
    sending: false,
    /** 当前流水线 trace 与节点状态。 */
    pipelineTraceId: '',
    pipelineNodes: freshPipelineNodes(),
    /** 有工具执行时安全流水线入口呼吸灯。 */
    pipelineHot: false,
    pipelinePanelVisible: false,
  }),

  getters: {
    /** 8 节点是否收到过真实事件（避免空面板误导）。 */
    pipelineHasEvents(state): boolean {
      return state.pipelineNodes.some((n) => n.status !== 'idle');
    },
  },

  actions: {
    setMode(mode: 'chat' | 'task'): void {
      this.mode = mode;
    },

    togglePipelinePanel(): void {
      this.pipelinePanelVisible = !this.pipelinePanelVisible;
    },

    clearMessages(): void {
      this.messages = [];
    },

    pushUserMessage(text: string): ChatMessage {
      const msg: ChatMessage = {
        id: makeId(),
        role: 'user',
        kind: 'text',
        text,
        toolEvents: [],
        createdAt: Date.now(),
      };
      this.messages.push(msg);
      return msg;
    },

    /**
     * SSE pipeline_step 事件入口：trace 变化时重置节点表，
     * 同 trace 内按 step 覆盖状态。
     */
    onPipelineEvent(ev: PipelineStepEvent): void {
      if (ev.trace_id !== this.pipelineTraceId) {
        this.pipelineTraceId = ev.trace_id;
        this.pipelineNodes = freshPipelineNodes();
        this.pipelineHot = false;
      }
      const node = this.pipelineNodes.find((n) => n.step === ev.step);
      if (node) {
        node.status = ev.status as PipelineNode['status'];
        node.detail = ev.detail || '';
        node.tool = ev.tool || '';
      }
      if (ev.step === 'execute' && ev.status === 'ok') {
        this.pipelineHot = true;
      }
    },

    /** 自然语言对话（≥180s 超时，409 冲突给出可读提示）。 */
    async sendChat(text: string): Promise<{ conflict: boolean; failed: boolean }> {
      const trimmed = (text || '').trim();
      if (!trimmed || this.sending) {
        return { conflict: false, failed: false };
      }
      this.pushUserMessage(trimmed);
      this.sending = true;
      try {
        const res = await butlerApi.chat(trimmed);
        this.messages.push({
          id: makeId(),
          role: 'butler',
          kind: 'text',
          text: res.reply,
          toolEvents: res.tool_events || [],
          createdAt: Date.now(),
        });
        if ((res.tool_events || []).length > 0) {
          this.pipelineHot = true;
        }
        return { conflict: false, failed: false };
      } catch (exc) {
        if (exc instanceof ApiConflictError) {
          return { conflict: true, failed: false };
        }
        return { conflict: false, failed: true };
      } finally {
        this.sending = false;
      }
    },

    /** 多步任务规划（≥180s 超时；渲染为步骤清单卡片）。 */
    async sendTask(text: string): Promise<{ conflict: boolean; failed: boolean }> {
      const trimmed = (text || '').trim();
      if (!trimmed || this.sending) {
        return { conflict: false, failed: false };
      }
      this.pushUserMessage(trimmed);
      this.sending = true;
      try {
        const report = await butlerApi.runTask(trimmed);
        this.messages.push({
          id: makeId(),
          role: 'butler',
          kind: 'task-report',
          text: report.summary,
          toolEvents: [],
          taskReport: report,
          createdAt: Date.now(),
        });
        return { conflict: false, failed: false };
      } catch (exc) {
        if (exc instanceof ApiConflictError) {
          return { conflict: true, failed: false };
        }
        return { conflict: false, failed: true };
      } finally {
        this.sending = false;
      }
    },
  },
});
