/**
 * 例行任务辅助测试（验收清单第 8 项：weekend 而非 weekends + 中文星期）。
 */
import { describe, expect, it } from 'vitest';
import {
  daysToApiValue,
  describeDays,
  describeLastResult,
  describeRoutineAction,
} from '@/services/routines';

describe('describeDays 中文展示', () => {
  it('0..6 整数数组 → 周一..周日（0=周一）', () => {
    expect(describeDays([0])).toBe('周一');
    expect(describeDays([1, 3, 5])).toBe('周二、周四、周六');
    expect(describeDays([6])).toBe('周日');
  });

  it('预设字符串直译', () => {
    expect(describeDays('daily')).toBe('每天');
    expect(describeDays('weekdays')).toBe('工作日');
    expect(describeDays('weekend')).toBe('周末');
  });

  it('完整周与典型组合收敛为简写', () => {
    expect(describeDays([0, 1, 2, 3, 4, 5, 6])).toBe('每天');
    expect(describeDays([0, 1, 2, 3, 4])).toBe('工作日');
    expect(describeDays([5, 6])).toBe('周末');
  });

  it('空值安全', () => {
    expect(describeDays(undefined)).toBe('未设置');
    expect(describeDays([])).toBe('未设置');
  });
});

describe('daysToApiValue（服务端契约：daily/weekdays/weekend 或 0..6 数组）', () => {
  it('全选 → daily（非 weekends！）', () => {
    expect(daysToApiValue([0, 1, 2, 3, 4, 5, 6])).toBe('daily');
  });
  it('周一至五 → weekdays', () => {
    expect(daysToApiValue([0, 1, 2, 3, 4])).toBe('weekdays');
  });
  it('周六日 → weekend（拼写正确）', () => {
    expect(daysToApiValue([5, 6])).toBe('weekend');
    expect(daysToApiValue([6, 5])).toBe('weekend');
  });
  it('任意组合 → 0..6 整数数组（去重排序）', () => {
    expect(daysToApiValue([6, 0, 6, 2])).toEqual([0, 2, 6]);
  });
  it('空选集 → 返回空数组（服务端拒绝 + UI 禁止提交，绝不放大成 daily）', () => {
    expect(daysToApiValue([])).toEqual([]);
  });
});

describe('动作摘要', () => {
  it('设备名解析 + value 展示', () => {
    const text = describeRoutineAction(
      { device_id: 'light.a', action: 'set_brightness', value: 40 },
      (id) => (id === 'light.a' ? '客厅灯' : ''),
    );
    expect(text).toBe('客厅灯 set_brightness 40');
  });

  it('无 value 时不带值；设备缺失回退 device_id', () => {
    const text = describeRoutineAction(
      { device_id: 'cover.b', action: 'open' },
      () => '',
    );
    expect(text).toBe('cover.b open');
  });
});

describe('last_result 展示', () => {
  it('空结果显示"尚未执行过"', () => {
    expect(describeLastResult('')).toBe('尚未执行过');
    expect(describeLastResult(null)).toBe('尚未执行过');
  });
  it('有结果原样透传', () => {
    expect(describeLastResult('2/2 步成功')).toBe('2/2 步成功');
  });
});
