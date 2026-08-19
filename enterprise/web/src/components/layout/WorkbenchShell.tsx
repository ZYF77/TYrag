import { useCallback, useEffect, useState, type ReactNode } from 'react';

export interface WorkbenchNavItem {
  id: string;
  label: string;
}

export interface WorkbenchNavGroup {
  id: string;
  label: string;
  items: WorkbenchNavItem[];
}

interface WorkbenchShellProps {
  testId: string;
  shellClass: string;
  brand: string;
  subtitle: string;
  actions: ReactNode;
  tokenRow: ReactNode;
  groups: WorkbenchNavGroup[];
  activeId: string;
  onSelect: (id: string) => void;
  children: ReactNode;
  footer?: ReactNode;
}

export function useWorkbenchTab<T extends string>(
  fallback: T,
  allowed: readonly T[],
): [T, (id: T) => void] {
  const read = useCallback((): T => {
    const raw = window.location.hash.replace(/^#\/?/, '');
    return allowed.includes(raw as T) ? (raw as T) : fallback;
  }, [allowed, fallback]);

  const [tab, setTab] = useState<T>(read);

  useEffect(() => {
    const onHash = () => setTab(read());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, [read]);

  const select = useCallback(
    (id: T) => {
      setTab(id);
      const nextHash = id === fallback ? '' : `#/${id}`;
      const next = `${window.location.pathname}${window.location.search}${nextHash}`;
      const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      if (current !== next) window.history.replaceState(null, '', next);
    },
    [fallback],
  );

  return [tab, select];
}

export function WorkbenchShell({
  testId,
  shellClass,
  brand,
  subtitle,
  actions,
  tokenRow,
  groups,
  activeId,
  onSelect,
  children,
  footer,
}: WorkbenchShellProps) {
  const activeGroup = groups.find((group) => group.items.some((item) => item.id === activeId));
  const activeItem = activeGroup?.items.find((item) => item.id === activeId);

  return (
    <div data-testid={testId} className={`${shellClass} workbench`}>
      <aside className="workbench-side" aria-label="功能菜单">
        <div className="workbench-brand">
          <span className="workbench-brand-mark" aria-hidden="true"><i /></span>
          <span className="workbench-brand-copy">
            <strong>{brand}</strong>
            <small>{subtitle}</small>
          </span>
        </div>
        <nav className="workbench-nav">
          {groups.map((group) => (
            <section key={group.id} className="workbench-group">
              <p className="workbench-group-label">{group.label}</p>
              {group.items.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={`workbench-item ${item.id === activeId ? 'is-active' : ''}`}
                  aria-current={item.id === activeId ? 'page' : undefined}
                  onClick={() => onSelect(item.id)}
                >
                  {item.label}
                </button>
              ))}
            </section>
          ))}
        </nav>
      </aside>
      <div className="workbench-main">
        <header className="console-nav">
          <div className="console-nav-inner">
            <div className="console-nav-top">
              <div className="workbench-context">
                <span>{brand} / {activeGroup?.label ?? '工作台'}</span>
                <strong data-testid="workbench-active-title">{activeItem?.label ?? brand}</strong>
              </div>
              <div className="console-nav-actions">{actions}</div>
            </div>
            {tokenRow}
          </div>
        </header>
        <div className="console-body workbench-pane">{children}</div>
        {footer}
      </div>
    </div>
  );
}
