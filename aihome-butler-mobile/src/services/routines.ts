/**
 * 例行任务辅助：周期（days）归一化与中文展示。
 *
 * 服务端契约（butler/proactive.py normalize_days）只接受
 * daily / weekdays / weekend 或 0..6 整数数组——**没有 weekends**（验收清单
 * 第 8 项专门盯这个）。星期编号 0=周一 … 6=周日。
 */

export type DaysPreset = 'daily' | 'weekdays' | 'weekend';

export const WEEKDAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'] as const;

/** 把 0..6 数组格式化为可读中文（0=周一）。 */
export function describeDays(days: number[] | string | undefined): string {
  if (typeof days === 'string') {
    if (days === 'daily') return '每天';
    if (days === 'weekdays') return '工作日';
    if (days === 'weekend') return '周末';
    return days;
  }
  if (!Array.isArray(days) || days.length === 0) {
    return '未设置';
  }
  if (days.length === 7) return '每天';
  const sorted = [...days].sort((a, b) => a - b);
  if (sorted.join(',') === '0,1,2,3,4') return '工作日';
  if (sorted.join(',') === '5,6') return '周末';
  return sorted.map((d) => WEEKDAY_LABELS[d] ?? '').filter(Boolean).join('、');
}

/**
 * UI 周期选择 → API days 字段：
 * 全选=每天 → 'daily'；周一至五 → 'weekdays'；周六日 → 'weekend'；
 * 其余任意组合 → 0..6 整数数组。绝不输出非法字符串。
 * 空选集返回 []（服务端会拒绝非法 days，UI 层 canSubmit 保证不会提交空集
 * ——这里故意不兜底成 daily，避免"忘了选日期"被放大成"每天执行"）。
 */
export function daysToApiValue(selected: number[]): DaysPreset | number[] {
  const unique = [...new Set(selected)].filter((d) => Number.isInteger(d) && d >= 0 && d <= 6);
  const sorted = unique.sort((a, b) => a - b);
  const key = sorted.join(',');
  if (key === '0,1,2,3,4,5,6') return 'daily';
  if (key === '0,1,2,3,4') return 'weekdays';
  if (key === '5,6') return 'weekend';
  return sorted;
}

/** 动作步骤的可读摘要（设备名未提供时回退 device_id）。 */
export function describeRoutineAction(
  step: { device_id: string; action: string; value?: unknown },
  deviceNameById: (id: string) => string,
): string {
  const name = deviceNameById(step.device_id) || step.device_id;
  const hasValue = step.value !== undefined && step.value !== null && step.value !== '';
  return hasValue ? `${name} ${step.action} ${String(step.value)}` : `${name} ${step.action}`;
}

/** 例行任务 last_result 收敛为列表标签用的短文本（空值显示从未执行）。 */
export function describeLastResult(result: string | null | undefined): string {
  const text = (result || '').trim();
  return text || '尚未执行过';
}
