/**
 * 对话/多步任务与 8 步安全流水线状态机测试（MOBILE_APP_SPEC.md §3.3）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { installUniMock, resetHttp, setupPinia, type UniMock } from './helpers';

vi.mock('@/services/butlerApi', () => ({
  chat: vi.fn(),
  runTask: vi.fn(),
}));

import * as butlerApi from '@/services/butlerApi';
import { freshPipelineNodes, PIPELINE_STEPS, useChatStore } from '@/stores/chat';
import { ApiConflictError } from '@/services/http';

let mock: UniMock;

beforeEach(async () => {
  mock = installUniMock();
  await resetHttp();
  await setupPinia();
  vi.clearAllMocks();
});

describe('8 步流水线固定节点表', () => {
  it('节点顺序与规格 §3.3 完全一致', () => {
    expect(PIPELINE_STEPS.map((s) => s.step)).toEqual([
      'register_check',
      'json_parse',
      'schema_validate',
      'risk_check',
      'dedup_check',
      'user_confirm',
      'execute',
      'verify_audit',
    ]);
    expect(PIPELINE_STEPS.map((s) => s.label)).toEqual([
      '白名单', 'JSON解析', 'Schema校验', '风险拦截',
      '3s防重放', '用户确认', '超时执行', '回读与审计',
    ]);
  });

  it('freshPipelineNodes 全部 idle 且含 8 节点', () => {
    const nodes = freshPipelineNodes();
    expect(nodes).toHaveLength(8);
    expect(nodes.every((n) => n.status === 'idle')).toBe(true);
  });
});

describe('pipeline_step 事件状态机', () => {
  it('trace 变化时节点重置；同 trace 内按 step 覆盖', () => {
    const chat = useChatStore();
    chat.onPipelineEvent({
      trace_id: 't1', tool: 'control_device', step: 'register_check',
      status: 'ok', detail: '已注册', actor: 'device_app:cli_x',
    });
    expect(chat.pipelineNodes[0].status).toBe('ok');
    expect(chat.pipelineTraceId).toBe('t1');

    chat.onPipelineEvent({
      trace_id: 't2', tool: 'control_device', step: 'risk_check',
      status: 'warn', detail: 'medium 需确认', actor: 'device_app:cli_x',
    });
    // trace 变化 → 全部重置为 idle，再写入 risk_check
    expect(chat.pipelineTraceId).toBe('t2');
    expect(chat.pipelineNodes[0].status).toBe('idle');
    expect(chat.pipelineNodes[3].status).toBe('warn');
    expect(chat.pipelineNodes[3].detail).toBe('medium 需确认');
  });

  it('execute ok 触发流水线呼吸灯', () => {
    const chat = useChatStore();
    expect(chat.pipelineHot).toBe(false);
    chat.onPipelineEvent({
      trace_id: 't1', tool: 'control_device', step: 'execute',
      status: 'ok', detail: '', actor: 'agent',
    });
    expect(chat.pipelineHot).toBe(true);
  });

  it('unknown 回读 → verify_audit error（诚实语义）', () => {
    const chat = useChatStore();
    chat.onPipelineEvent({
      trace_id: 't1', tool: 'control_device', step: 'verify_audit',
      status: 'error', detail: '回读失败', actor: 'agent',
    });
    expect(chat.pipelineNodes[7].status).toBe('error');
  });
});

describe('对话发送', () => {
  it('成功：压入用户与管家消息并携带工具事件', async () => {
    const chat = useChatStore();
    vi.mocked(butlerApi.chat).mockResolvedValue({
      reply: '已打开客厅灯',
      tool_events: [{ tool: 'control_device', status: 'ok', message: '执行成功' }],
      notifications: [],
    });
    const res = await chat.sendChat('打开客厅灯');
    expect(res.failed).toBe(false);
    expect(chat.messages).toHaveLength(2);
    expect(chat.messages[0].role).toBe('user');
    expect(chat.messages[1].role).toBe('butler');
    expect(chat.messages[1].toolEvents).toHaveLength(1);
  });

  it('409 冲突：返回 conflict 标记，不压入失败消息', async () => {
    const chat = useChatStore();
    vi.mocked(butlerApi.chat).mockRejectedValue(new ApiConflictError('已有对话在处理中'));
    const res = await chat.sendChat('开灯');
    expect(res.conflict).toBe(true);
    expect(chat.messages).toHaveLength(1); // 只有用户消息
  });

  it('发送中禁止重复发送', async () => {
    const chat = useChatStore();
    let resolveApi!: (v: unknown) => void;
    vi.mocked(butlerApi.chat).mockImplementation(
      () => new Promise((resolve) => {
        resolveApi = resolve;
      }),
    );
    const first = chat.sendChat('第一条');
    const second = await chat.sendChat('第二条');
    expect(second.failed).toBe(false);
    expect(second.conflict).toBe(false);
    expect(chat.messages).toHaveLength(1); // 第二条未入队
    // 放行第一条请求，验证正常收尾（用户消息 + 管家回复，第二条从未入队）
    resolveApi({ reply: '好的', tool_events: [], notifications: [] });
    await first;
    expect(chat.messages).toHaveLength(2);
    expect(chat.messages[1].text).toBe('好的');
  });
});

describe('多步任务', () => {
  it('报告渲染数据透传', async () => {
    const chat = useChatStore();
    vi.mocked(butlerApi.runTask).mockResolvedValue({
      status: 'completed',
      steps: [
        { tool: 'control_device', status: 'ok', message: '已开灯' },
        { tool: 'control_device', status: 'ok', message: '已关窗帘' },
      ],
      summary: '2 步全部完成',
    });
    const res = await chat.sendTask('睡眠场景');
    expect(res.failed).toBe(false);
    const msg = chat.messages[1];
    expect(msg.kind).toBe('task-report');
    expect(msg.taskReport?.steps).toHaveLength(2);
    expect(msg.taskReport?.summary).toBe('2 步全部完成');
  });
});
