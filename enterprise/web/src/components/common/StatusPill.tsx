import { NOT_PROVIDED } from '../../lib/format';

/**
 * 业务状态色板：绿=正常、红=失败、橙=需关注、蓝=处理中、灰=其他。
 * 取 Console 元数据表与会话气泡两处 tone 映射的并集；未知码原样展示。
 */
const STATUS_PILL_TONES: Record<string, string> = {
  ready: 'ok',
  active: 'ok',
  completed: 'ok',
  '已完成': 'ok',
  failed: 'failed',
  '失败': 'failed',
  review_required: 'warn',
  no_reliable_evidence: 'warn',
  '无可靠依据': 'warn',
  parsing: 'processing',
  processing: 'processing',
  running: 'processing',
  streaming: 'processing',
  registered: 'muted',
  cancelled: 'muted',
  archived: 'muted',
  superseded: 'muted',
  disabled: 'muted',
};

export function StatusPill({ code, label }: { code: string | null | undefined; label?: string }) {
  const value = code ?? '';
  const tone = STATUS_PILL_TONES[value] ?? 'muted';
  return (
    <span className={`console-status console-status--${tone}`}>
      <span className="console-status-dot" aria-hidden="true" />
      {label ?? (value || NOT_PROVIDED)}
    </span>
  );
}
