/**
 * Butler 服务端全量 API 方法封装（MOBILE_APP_SPEC.md §4.1 端点速查表）。
 *
 * 契约纪律：方法名/路径/字段一律对照 docs/MOBILE_APP_SPEC.md 与
 * butler/webapp.py 核验，禁止自造不存在的接口或改字段名。
 */
import { request, requestBinary, TIMEOUTS } from './http';
import type {
  AuditLogItem,
  ChatResponse,
  ConfirmationItem,
  DeviceView,
  DirectCommandResponse,
  GarmentItem,
  HealthResponse,
  MemoryItem,
  ModelProfileView,
  NotificationItem,
  OverviewResponse,
  PairedDeviceItem,
  PantryItem,
  PairConfirmResponse,
  PairStartResponse,
  PersonaResponse,
  ProactiveRuleItem,
  RoutineCreateRequest,
  RoutineItem,
  SpeakersStatusResponse,
  SystemInfoResponse,
  TaskLogItem,
  TaskReport,
  VoiceStatusResponse,
} from '@/types/api';

// ---------------- 探针 / 系统 ----------------

export function getHealth(baseUrl?: string): Promise<HealthResponse> {
  return request<HealthResponse>('/api/health', { method: 'GET', auth: false, baseUrl });
}

export function getSystemInfo(): Promise<SystemInfoResponse> {
  return request<SystemInfoResponse>('/api/system/info');
}

export function getOverview(): Promise<OverviewResponse> {
  return request<OverviewResponse>('/api/overview');
}

// ---------------- 设备与直控 ----------------

export function getDevices(): Promise<{ devices: DeviceView[] }> {
  return request<{ devices: DeviceView[] }>('/api/devices');
}

export function getDeviceState(deviceId: string): Promise<DeviceView> {
  return request<DeviceView>(`/api/devices/${encodeURIComponent(deviceId)}/state`);
}

/** 设备直控：{action, value?}，无 value 时省略该键（服务端 value=None 视为未传）。 */
export function sendDeviceCommand(
  deviceId: string,
  action: string,
  value?: string | number,
): Promise<DirectCommandResponse> {
  const body: Record<string, unknown> = { action };
  if (value !== undefined && value !== null && value !== '') {
    body['value'] = value;
  }
  return request<DirectCommandResponse>(
    `/api/devices/${encodeURIComponent(deviceId)}/command`,
    { method: 'POST', data: body },
  );
}

// ---------------- 配对 ----------------

export function pairStart(): Promise<PairStartResponse> {
  return request<PairStartResponse>('/api/pair/start', { method: 'POST' });
}

export function pairConfirm(
  code: string,
  deviceName: string,
  platform: string,
): Promise<PairConfirmResponse> {
  return request<PairConfirmResponse>('/api/pair/confirm', {
    method: 'POST',
    auth: false,
    data: { code, device_name: deviceName, platform },
  });
}

export function listPairedDevices(): Promise<{ devices: PairedDeviceItem[] }> {
  return request<{ devices: PairedDeviceItem[] }>('/api/pair/devices');
}

export function revokePairedDevice(deviceId: string): Promise<{ device_id: string; revoked: boolean }> {
  return request<{ device_id: string; revoked: boolean }>(
    `/api/pair/devices/${encodeURIComponent(deviceId)}`,
    { method: 'DELETE' },
  );
}

// ---------------- 对话 / 多步任务 / 确认 ----------------

/** 对话：阻塞等待（含确认等待），超时必须 ≥180s（确认队列最长等 120s）。 */
export function chat(text: string): Promise<ChatResponse> {
  return request<ChatResponse>('/api/chat', {
    method: 'POST',
    data: { text },
    timeout: TIMEOUTS.CHAT,
  });
}

/** 多步规划：返回计划执行报告，超时必须 ≥180s。 */
export function runTask(text: string): Promise<TaskReport> {
  return request<TaskReport>('/api/task', {
    method: 'POST',
    data: { text },
    timeout: TIMEOUTS.TASK,
  });
}

export function getConfirmations(): Promise<{ confirmations: ConfirmationItem[] }> {
  return request<{ confirmations: ConfirmationItem[] }>('/api/confirmations');
}

export function respondConfirmation(
  confirmationId: string,
  approved: boolean,
): Promise<{ resolved: boolean; approved: boolean }> {
  return request<{ resolved: boolean; approved: boolean }>(
    `/api/confirmations/${encodeURIComponent(confirmationId)}`,
    { method: 'POST', data: { approved } },
  );
}

// ---------------- 例行任务 ----------------

export function listRoutines(): Promise<{ routines: RoutineItem[] }> {
  return request<{ routines: RoutineItem[] }>('/api/routines');
}

/** 创建例行任务：days 严格传 daily/weekdays/weekend 或 0..6 整数数组（切勿传 weekends）。 */
export function createRoutine(
  body: RoutineCreateRequest,
): Promise<{ routine_id: number; created: boolean; steps: string[] }> {
  return request<{ routine_id: number; created: boolean; steps: string[] }>(
    '/api/routines',
    { method: 'POST', data: body as unknown as Record<string, unknown> },
  );
}

export function toggleRoutine(routineId: number): Promise<{ routine_id: number; enabled: boolean }> {
  return request<{ routine_id: number; enabled: boolean }>(
    `/api/routines/${routineId}/toggle`,
    { method: 'POST' },
  );
}

export function deleteRoutine(routineId: number): Promise<{ routine_id: number; deleted: boolean }> {
  return request<{ routine_id: number; deleted: boolean }>(
    `/api/routines/${routineId}`,
    { method: 'DELETE' },
  );
}

// ---------------- 通知 / 规则 / 任务日志 ----------------

export function listNotifications(): Promise<{ notifications: NotificationItem[] }> {
  return request<{ notifications: NotificationItem[] }>('/api/notifications');
}

export function listRules(): Promise<{ rules: ProactiveRuleItem[] }> {
  return request<{ rules: ProactiveRuleItem[] }>('/api/rules');
}

export function listTaskLogs(): Promise<{ tasks: TaskLogItem[] }> {
  return request<{ tasks: TaskLogItem[] }>('/api/tasks');
}

// ---------------- 记忆 ----------------

export function listMemories(): Promise<{ memories: MemoryItem[] }> {
  return request<{ memories: MemoryItem[] }>('/api/memories');
}

export function addMemory(kind: 'preference' | 'fact', content: string): Promise<{ id: number; saved: boolean }> {
  return request<{ id: number; saved: boolean }>('/api/memories', {
    method: 'POST',
    data: { kind, content },
  });
}

export function deleteMemory(memoryId: number): Promise<{ deleted: boolean }> {
  return request<{ deleted: boolean }>(`/api/memories/${memoryId}`, { method: 'DELETE' });
}

// ---------------- 家庭台账（未装配时服务端 503） ----------------

export function getPantry(): Promise<{ items: PantryItem[]; count: number }> {
  return request<{ items: PantryItem[]; count: number }>('/api/pantry');
}

export function getWardrobe(): Promise<{ garments: GarmentItem[]; count: number }> {
  return request<{ garments: GarmentItem[]; count: number }>('/api/wardrobe');
}

// ---------------- 设置 ----------------

export function getPersona(): Promise<PersonaResponse> {
  return request<PersonaResponse>('/api/settings/persona');
}

export function updatePersona(persona: string): Promise<{ persona: string; saved: boolean }> {
  return request<{ persona: string; saved: boolean }>('/api/settings/persona', {
    method: 'PUT',
    data: { persona },
  });
}

export function getModelProfiles(): Promise<{ profiles: ModelProfileView[] }> {
  return request<{ profiles: ModelProfileView[] }>('/api/settings/models');
}

export function getAudit(limit = 100): Promise<{ audit: AuditLogItem[] }> {
  return request<{ audit: AuditLogItem[] }>(`/api/audit?limit=${Math.max(1, Math.min(limit, 500))}`);
}

// ---------------- 语音与声纹（未装配时 503） ----------------

export function getVoiceStatus(): Promise<VoiceStatusResponse> {
  return request<VoiceStatusResponse>('/api/voice/status');
}

/** 文本转 WAV：返回 audio/wav 二进制（arraybuffer）。 */
export function synthesizeTts(text: string): Promise<ArrayBuffer> {
  return requestBinary('/api/tts', {
    method: 'POST',
    data: { text },
    // 服务端合成可能较慢（CosyVoice 推理），放宽到 60s
    timeout: 60000,
  });
}

export function getSpeakersStatus(): Promise<SpeakersStatusResponse> {
  return request<SpeakersStatusResponse>('/api/speakers');
}
