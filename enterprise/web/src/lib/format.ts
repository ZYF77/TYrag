export const NOT_PROVIDED = '未提供';

/** 完整本地时间；无效或缺失时原样返回（缺失显示“未提供”）。 */
export function formatTime(value: string | null | undefined): string {
  if (!value) return NOT_PROVIDED;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

/** 运行日志用的短格式：月/日 时:分:秒；无效时原样返回。 */
export function formatTimeShort(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    });
}

export function formatValue(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return NOT_PROVIDED;
  return String(value);
}

export function formatMiB(value: number | null | undefined): string {
  if (value === null || value === undefined) return NOT_PROVIDED;
  const mib = value / (1024 * 1024);
  return `${Number.isInteger(mib) ? mib : mib.toFixed(2)} MiB`;
}
