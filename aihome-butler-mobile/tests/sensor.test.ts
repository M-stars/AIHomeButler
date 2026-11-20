/**
 * 传感器字段兼容测试（MOBILE_APP_SPEC.md §3.2 第 2 条 / 验收清单第 3 项）：
 * GPIO/模拟后端 temperature_c/humidity_percent/motion_detected 与
 * HA 标量 value+unit 均正确显示；fresh/available 徽章语义正确。
 */
import { describe, expect, it } from 'vitest';
import { extractSensorReadings } from '@/services/sensor';
import type { DeviceView } from '@/types/api';

function makeDevice(overrides: Partial<DeviceView>): DeviceView {
  return {
    id: 'sensor.test',
    name: '测试传感器',
    type: 'sensor',
    area: '客厅',
    available: true,
    actions: [],
    state: {},
    ...overrides,
  };
}

describe('extractSensorReadings 字段兼容', () => {
  it('GPIO/模拟后端：temperature_c + humidity_percent + motion_detected', () => {
    const d = makeDevice({
      state: { temperature_c: 23.46, humidity_percent: 55.4, motion_detected: true },
    });
    const readings = extractSensorReadings(d);
    const labels = readings.map((r) => r.label);
    expect(labels).toContain('温度');
    expect(labels).toContain('湿度');
    expect(labels).toContain('人体');
    expect(readings.find((r) => r.label === '温度')?.display).toBe('23.5 °C');
    expect(readings.find((r) => r.label === '湿度')?.display).toBe('55.4 %');
    expect(readings.find((r) => r.label === '人体')?.display).toBe('有人移动');
  });

  it('HA 兜底字段：temperature + humidity + motion(0/1)', () => {
    const d = makeDevice({
      state: { temperature: 26, humidity: 60, motion: 0 },
    });
    const readings = extractSensorReadings(d);
    expect(readings.find((r) => r.label === '温度')?.display).toBe('26 °C');
    expect(readings.find((r) => r.label === '湿度')?.display).toBe('60 %');
    expect(readings.find((r) => r.label === '人体')?.display).toBe('无人');
  });

  it('HA 标量传感器：value + unit', () => {
    const d = makeDevice({
      name: '光照传感器',
      state: { value: 312, unit: 'lx' },
    });
    const readings = extractSensorReadings(d);
    expect(readings).toHaveLength(1);
    expect(readings[0].display).toBe('312 lx');
  });

  it('temperature_c 优先于 temperature（不重复展示）', () => {
    const d = makeDevice({
      state: { temperature_c: 20, temperature: 99, value: 20, unit: '°C' },
    });
    const readings = extractSensorReadings(d);
    const temps = readings.filter((r) => r.display.includes('20'));
    expect(readings.find((r) => r.label === '温度')?.display).toBe('20 °C');
    // value=20 °C 与温度重复：去重后只剩一条
    expect(temps).toHaveLength(1);
  });

  it('available=false → offline 徽章（红色离线）', () => {
    const d = makeDevice({ available: false, state: { temperature_c: 21 } });
    const readings = extractSensorReadings(d);
    expect(readings[0].badge).toBe('offline');
  });

  it('fresh=false → stale 徽章（数据陈旧）', () => {
    const d = makeDevice({ state: { temperature_c: 21, fresh: false } });
    const readings = extractSensorReadings(d);
    expect(readings[0].badge).toBe('stale');
  });

  it('available=false 优先于 fresh=false', () => {
    const d = makeDevice({ available: false, state: { temperature_c: 21, fresh: false } });
    const readings = extractSensorReadings(d);
    expect(readings[0].badge).toBe('offline');
  });

  it('无 state → 空读数（不崩溃）', () => {
    const d = makeDevice({ state: null });
    expect(extractSensorReadings(d)).toEqual([]);
  });
});
