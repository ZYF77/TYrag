import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { ChevronDown, Grid2X2, Settings2 } from 'lucide-react';

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
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(
    () => new Set(activeGroup ? [activeGroup.id] : []),
  );

  useEffect(() => {
    if (!activeGroup) return;
    setExpandedGroups((current) => current.has(activeGroup.id)
      ? current
      : new Set(current).add(activeGroup.id));
  }, [activeGroup]);

  const toggleGroup = (groupId: string) => {
    setExpandedGroups((current) => {
      const next = new Set(current);
      if (next.has(groupId)) next.delete(groupId);
      else next.add(groupId);
      return next;
    });
  };

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
        <nav className="workbench-surface-tabs" aria-label="工作台切换">
          <a href="/console" className={`workbench-surface-tab ${brand === 'Console' ? 'is-active' : ''}`} aria-current={brand === 'Console' ? 'page' : undefined}>
            <Settings2 size={16} aria-hidden="true" />
            Console
          </a>
          <a href="/" className={`workbench-surface-tab ${brand === 'Harness' ? 'is-active' : ''}`} aria-current={brand === 'Harness' ? 'page' : undefined}>
            <Grid2X2 size={16} aria-hidden="true" />
            Harness
          </a>
        </nav>
        <nav className="workbench-nav">
          {groups.map((group) => {
            const expanded = expandedGroups.has(group.id);
            const itemsId = `workbench-group-${group.id}`;
            return (
              <section key={group.id} className="workbench-group">
                <button
                  type="button"
                  className="workbench-group-toggle"
                  aria-expanded={expanded}
                  aria-controls={itemsId}
                  onClick={() => toggleGroup(group.id)}
                >
                  <span className="workbench-group-label">{group.label}</span>
                  <ChevronDown size={15} aria-hidden="true" className={`workbench-group-chevron ${expanded ? 'is-open' : ''}`} />
                </button>
                <div id={itemsId} className="workbench-group-items" hidden={!expanded}>
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
                </div>
              </section>
            );
          })}
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
