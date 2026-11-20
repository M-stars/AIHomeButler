/**
 * 传感器多后端字段兼容读取（MOBILE_APP_SPEC.md §3.2 第 2 条，验收清单第 3 项）。
 *
 * 关键取值优先级（务必按此顺序兜底，禁止只读单一字段名）：
 *   温度：state.temperature_c ?? state.temperature   （单位 °C）
 *   湿度：state.humidity_percent ?? state.humidity   （单位 %）
 *   通用标量：state.value + state.unit
 *   人体感应：state.motion_detected ?? state.motion  （有人移动/无人）
 *
 * 新鲜度：state.fresh === false →「数据陈旧」橙色徽章；
 * 设备离线：available === false →「离线」红色徽章。
 */
import type { DeviceState, DeviceView } from '@/types/api';

export interface SensorReading {
  /** 展示标签，如「温度」「湿度」「人体」「光照」。 */
  label: string;
  /** 已格式化好的展示值，如「23.5 °C」「有人移动」。 */
  display: string;
  /** 徽章：stale=数据陈旧（橙），offline=设备离线（红）。 */
  badge: 'stale' | 'offline' | null;
}

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && isFinite(v);
}

/** 格式化数值：最多保留 1 位小数，去掉多余的 .0。 */
function fmtNumber(v: number): string {
  const rounded = Math.round(v * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

/**
 * 从单个传感器设备状态提取 0..N 条可读读数。
 * 布尔人体感应、数值型温湿度与任意标量（带 unit）都归一为 SensorReading。
 */
export function extractSensorReadings(device: DeviceView): SensorReading[] {
  const state: DeviceState | null = device.state;
  const badge = device.available === false ? 'offline' : (state?.fresh === false ? 'stale' : null);
  if (!state) {
    return [];
  }
  const readings: SensorReading[] = [];

  // 温度：temperature_c 优先（GPIO/模拟后端），temperature 兜底（HA 标量/温控）
  const temp = state.temperature_c ?? state.temperature;
  if (isFiniteNumber(temp)) {
    readings.push({ label: '温度', display: `${fmtNumber(temp)} °C`, badge });
  }

  // 湿度：humidity_percent 优先，humidity 兜底
  const humidity = state.humidity_percent ?? state.humidity;
  if (isFiniteNumber(humidity)) {
    readings.push({ label: '湿度', display: `${fmtNumber(humidity)} %`, badge });
  }

  // 人体感应：motion_detected 优先，motion 兜底（boolean 或 0/1）
  const rawMotion = state.motion_detected ?? (state as { motion?: unknown }).motion;
  if (rawMotion !== undefined && rawMotion !== null && typeof rawMotion !== 'object') {
    readings.push({
      label: '人体',
      display: rawMotion ? '有人移动' : '无人',
      badge,
    });
  }

  // 通用标量（HA 任意 unit 传感器：光照/电压/计数……）
  if (state.value !== undefined && state.value !== null && typeof state.value !== 'object') {
    const unit = state.unit ? ` ${state.unit}` : '';
    const valueText = isFiniteNumber(state.value) ? fmtNumber(state.value) : String(state.value);
    // 温湿度已展示过的标量不重复展示
    const duplicated = readings.some(
      (r) => (r.label === '温度' || r.label === '湿度') && r.display === `${valueText}${unit}`,
    );
    if (!duplicated) {
      readings.push({ label: device.name || '数值', display: `${valueText}${unit}`, badge });
    }
  }

  return readings;
}

/**
 * 传感器卡片时间戳（sampled_at 原样透传展示，无则不显示）。
 */
export function sensorSampledAt(device: DeviceView): string {
  const at = device.state?.sampled_at;
  return typeof at === 'string' ? at : '';
}
