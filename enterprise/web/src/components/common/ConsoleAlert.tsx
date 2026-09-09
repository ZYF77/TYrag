import type { DisplayError } from '../../api/v2Types';

/**
 * 简版错误行（`[code] message`）。标签与 role 按调用点现状选择：
 * Harness 侧为 <p>，Inspector 侧为 role="alert" 的 <div>。
 */
export function ConsoleAlert({
  error,
  as = 'p',
  withRole = false,
}: {
  error: DisplayError;
  as?: 'p' | 'div';
  withRole?: boolean;
}) {
  const Tag = as;
  return (
    <Tag className="console-alert" role={withRole ? 'alert' : undefined}>
      [{error.code}] {error.message}
    </Tag>
  );
}
