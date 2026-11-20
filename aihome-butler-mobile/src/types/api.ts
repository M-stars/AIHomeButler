/**
 * 后端 API 完整类型定义（MOBILE_APP_SPEC.md §4.2）。
 *
 * 所有字段已对照 `butler/webapp.py`、`butler/devices/*`、`butler/memory/store.py`
 * 逐行核验；仅服务端确实返回的字段才收录，避免前端编造契约。
 */

export type DeviceType =
  | 'sensor' | 'light' | 'fan' | 'switch'
  | 'cover' | 'climate' | 'humidifier' | 'vacuum';

export interface HealthResponse {
  status: 'ok';
  service: string;
  version: string;
  protocol_version: string;
  uptime_seconds: number;
}

export interface CircuitBreakerStatus {
  state: 'closed' | 'open' | 'half_open';
  consecutive_failures: number;
  failure_threshold: number;
  recovery_timeout: number;
  retry_after_seconds: number;
}

export interface SystemInfoResponse {
  version: string;
  protocol_version: string;
  model: string;
  backend: 'simulated' | 'homeassistant' | 'gpio';
  confirmation_mode: 'always' | 'never';
  tts_backend: string;
  uptime_seconds: number;
  ha_circuit_breaker: CircuitBreakerStatus | null;
  paired_devices_count: number;
  capabilities: {
    direct_control: boolean;
    sse_events: boolean;
    pairing: boolean;
    routines: boolean;
    planner: boolean;
    voice: boolean;
    speakers: boolean;
    household: boolean;
  };
}

export interface DeviceState {
  // 开关/亮度/风速/窗帘/温控/加湿/扫地机
  on?: boolean;
  brightness?: number;          // 1-100
  speed?: 'low' | 'medium' | 'high';
  open?: boolean;
  position?: number | null;     // 0-100
  activity?: string;
  hvac_mode?: string;
  current_temperature?: number | null;
  temperature?: number | null;
  humidity?: number | null;
  current_humidity?: number | null;
  status?: string;
  battery?: number | null;
  // 传感器（跨后端兼容字段）
  temperature_c?: number;
  humidity_percent?: number;
  motion_detected?: boolean;
  value?: number | string;
  unit?: string;
  sampled_at?: string;
  fresh?: boolean;
}

export interface DeviceView {
  id: string;
  name: string;
  type: DeviceType;
  area: string;
  available: boolean;
  actions: string[];
  state: DeviceState | null;
}

export interface TokenBudgetStatus {
  enabled: boolean;
  limit: number;
  used_today: number;
  remaining: number;
}

export interface OverviewResponse {
  devices: DeviceView[];
  model: string;
  backend: string;
  confirmation_mode: 'always' | 'never';
  tts_backend: string;
  token_budget: TokenBudgetStatus | null;
  ha_circuit_breaker: CircuitBreakerStatus | null;
}

export interface DirectCommandRequest {
  action: string;
  value?: string | number;
}

export interface DirectCommandResponse {
  device_id: string;
  action: string;
  status: 'ok' | 'unknown' | 'error';
  stage: 'verified' | 'done' | 'duplicate_skipped' | 'unknown' | 'failed' | 'cancelled' | 'executed';
  message: string;
  verified: boolean;
  state_after: DeviceState | null;
}

export interface PairStartResponse {
  code: string;
  expires_in_seconds: number;
  expires_at: string;
  max_attempts: number;
}

export interface PairConfirmRequest {
  code: string;
  device_name: string;
  platform: 'android' | 'ios' | 'miniprogram' | 'pc' | 'web';
}

export interface PairConfirmResponse {
  device_id: string;
  device_name: string;
  platform: string;
  token: string;
  protocol_version: string;
}

export interface PairedDeviceItem {
  device_id: string;
  device_name: string;
  platform: string;
  created_at: string;
  last_seen_at: string;
  revoked: boolean;
}

export interface ConfirmationItem {
  id: string;
  summary: string;
  arguments: Record<string, unknown>;
  created_at: string;
  approved: boolean;
}

export type PipelineStepName =
  | 'register_check' | 'json_parse' | 'schema_validate'
  | 'risk_check' | 'dedup_check' | 'user_confirm'
  | 'execute' | 'verify_audit';

export type PipelineStepStatus = 'ok' | 'warn' | 'error' | 'pending' | 'skipped';

export interface PipelineStepEvent {
  trace_id: string;
  tool: string;
  step: PipelineStepName;
  status: PipelineStepStatus;
  detail: string;
  actor: string;
}

export interface ToolEvent {
  tool: string;
  status: string;
  message: string;
}

export interface ChatResponse {
  reply: string;
  tool_events: ToolEvent[];
  notifications: Array<{ id?: number; message: string; level?: string }>;
}

export interface TaskStepReport {
  tool: string;
  status: string;
  message?: string;
}

export interface TaskReport {
  status: string;
  steps: TaskStepReport[];
  summary: string;
}

export interface RoutineItem {
  id: number;
  name: string;
  schedule: { time: string; days: number[] | string; catchup_minutes?: number };
  actions: Array<{ device_id: string; action: string; value?: unknown }>;
  notify_on_success: boolean;
  enabled: boolean;
  last_fired_at: string | null;
  last_result: string;
  created_at: string;
}

export interface RoutineCreateRequest {
  name: string;
  time: string;
  days: 'daily' | 'weekdays' | 'weekend' | number[];
  actions: Array<{ device_id: string; action: string; value?: string | number }>;
  notify_on_success?: boolean;
  catchup_minutes?: number;
}

export interface MemoryItem {
  id: number;
  kind: 'preference' | 'fact';
  content: string;
  source: 'user' | 'inferred';
  member_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface NotificationItem {
  id: number;
  created_at: string;
  rule_id: number | null;
  rule_name: string;
  level: string;
  message: string;
  read: boolean;
}

export interface ProactiveRuleItem {
  id: number;
  name: string;
  kind: 'sensor_threshold' | 'device_offline' | 'schedule';
  params: Record<string, unknown>;
  enabled: boolean;
  cooldown_seconds: number;
  last_fired_at: string | null;
}

export interface TaskLogItem {
  id: number;
  created_at: string;
  user_text: string;
  outcome: string;
  steps: TaskStepReport[];
}

export interface AuditLogItem {
  id: number;
  created_at: string;
  actor: string;
  action: string;
  detail: string;
  status: string;
}

export interface PersonaPreset {
  id: string;
  name: string;
  prompt: string;
  description: string;
}

export interface PersonaResponse {
  persona: string;
  presets: PersonaPreset[];
}

export interface ModelProfileView {
  name: string;
  base_url: string;
  model: string;
  api_key_masked: string;
  timeout_seconds: number;
}

export interface PantryItem {
  id: number;
  name: string;
  category: string;
  quantity: string;
  added_date: string;
  expires_in_days: number | null;
  days_left: number | null;
  expired: boolean;
}

export interface GarmentItem {
  id: number;
  owner: string;
  name: string;
  category: string;
  warmth: number;
  status: string;
  times_worn_since_wash: number;
  last_wash_date: string | null;
  wash_interval_days: number | null;
}

export interface SpeakerMember {
  member_id: string;
  display_name: string;
  voice_id: string | null;
  created_at: string;
}

export interface SpeakersStatusResponse {
  speaker_recognition: boolean;
  threshold: number;
  member_count: number;
  members: SpeakerMember[];
  service_healthy: boolean;
  ai_disclaimer: string;
}

export interface VoiceStatusResponse {
  backend: string;
  [key: string]: unknown;
}

// ---------------- SSE 事件载荷（对照 webapp.py EventHub.publish 调用点） ----------------

export interface DeviceUpdatedEvent {
  device_id: string;
  action: string;
  status: string;
  stage?: string;
  state: DeviceState | null;
}

export interface ConfirmationResolvedEvent {
  id: string;
  approved: boolean;
  summary: string;
}

export interface ConnectedEvent {
  status: 'connected';
  ts: string;
}

/** SSE 原始事件（EventHub.publish 的统一包裹结构之外，SSE 帧里 event 名称单独携带）。 */
export type ButlerSseEventName =
  | 'connected'
  | 'pipeline_step'
  | 'confirmation_pending'
  | 'confirmation_resolved'
  | 'device_updated'
  // 服务端当前版本未发布 notification 事件（通知走轮询 /api/notifications）；
  // 保留监听以兼容未来版本扩展（MOBILE_APP_SPEC.md §4.1）。
  | 'notification';
