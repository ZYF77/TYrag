import type { ReactNode } from 'react';
import type { ConsoleModuleStatus, ConsoleState } from '../../api/consoleTypes';
import type { DisplayError } from '../../api/v2Types';
import { cn } from '../../lib/cn';

export function initialPanelState<T>(): ConsoleState<T> {
  return { status: 'processing', data: null, error: null };
}

export function panelErrorStatus(error: DisplayError): ConsoleModuleStatus {
  if (error.httpStatus === 401 || error.httpStatus === 403) return 'unauthorized';
  if (error.httpStatus === 0 || error.httpStatus === 502 || error.httpStatus === 503) {
    return 'unavailable';
  }
  return 'failed';
}

export function PanelBadge({ status }: { status: ConsoleModuleStatus }) {
  return (
    <span className={`console-status console-status--${status}`}>
      <span className="console-status-dot" aria-hidden="true" />
      {status}
    </span>
  );
}

export function PanelCard({
  eyebrow,
  title,
  description,
  status,
  actions,
  children,
  testId,
  className,
}: {
  eyebrow: string;
  title: string;
  description: string;
  status: ConsoleModuleStatus;
  actions?: ReactNode;
  children: ReactNode;
  testId: string;
  className?: string;
}) {
  return (
    <section data-testid={testId} className={cn('console-card', className)}>
      <div className="console-card-head">
        <div>
          <p className="console-eyebrow">{eyebrow}</p>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
        <div className="console-card-actions">
          <PanelBadge status={status} />
          {actions}
        </div>
      </div>
      <div className="console-card-body">{children}</div>
    </section>
  );
}

export function PanelError({ error, onRetry }: { error: DisplayError; onRetry?: () => void }) {
  return (
    <div role="alert" className="console-alert">
      <p><strong>{error.code}</strong>{error.httpStatus ? ` · HTTP ${error.httpStatus}` : ''} · {error.message}</p>
      {onRetry && <button type="button" onClick={onRetry} className="console-secondary-button">重试</button>}
    </div>
  );
}
