import { Globe2 } from 'lucide-react';
import { cn } from '../../lib/cn';

/**
 * 开关控件。runtime 变体带“已启用/已停用”文案与 is-on 类；
 * harness 变体是带地球图标的 thumb（测试钉住 .harness-toggle-thumb svg）。
 */
export function ToggleSwitch({
  checked,
  label,
  onChange,
  disabled,
  variant = 'runtime',
}: {
  checked: boolean;
  /** runtime 变体为开关可访问名；harness 变体为 checkbox 的 aria-label。 */
  label: string;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  variant?: 'runtime' | 'harness';
}) {
  if (variant === 'harness') {
    return (
      <label className="harness-toggle">
        <input
          type="checkbox"
          aria-label={label}
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span className="harness-toggle-track" aria-hidden="true">
          <span className="harness-toggle-thumb"><Globe2 size={12} /></span>
        </span>
      </label>
    );
  }
  return (
    <label className={cn('runtime-toggle', checked && 'is-on')}>
      <input
        className="runtime-toggle-input"
        type="checkbox"
        checked={checked}
        aria-label={label}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="runtime-toggle-track" aria-hidden="true"><span /></span>
      <span className="runtime-toggle-text">{checked ? '已启用' : '已停用'}</span>
    </label>
  );
}
