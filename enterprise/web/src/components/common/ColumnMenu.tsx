import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Columns3 } from 'lucide-react';

export interface ConsoleColumnDefinition {
  key: string;
  label: string;
  /** 固定列在菜单里禁用勾选，且即使用户曾隐藏过也始终显示（如主键列、操作列）。 */
  fixed?: boolean;
}

function readHiddenColumns(storageKey: string, defaults: string[]): string[] {
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return defaults;
    const parsed: unknown = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return parsed.filter((value): value is string => typeof value === 'string');
    }
  } catch {
    // localStorage 不可用（隐私模式等）时退回默认预设。
  }
  return defaults;
}

export function useHiddenTableColumns<T extends ConsoleColumnDefinition>(
  storageKey: string,
  defaultHidden: string[],
  columns: ReadonlyArray<T>,
) {
  const [hiddenColumns, setHiddenColumns] = useState<string[]>(
    () => readHiddenColumns(storageKey, defaultHidden),
  );

  const toggleColumn = useCallback((key: string) => {
    setHiddenColumns((current) => {
      const next = current.includes(key)
        ? current.filter((value) => value !== key)
        : [...current, key];
      try {
        localStorage.setItem(storageKey, JSON.stringify(next));
      } catch {
        // 隐私模式下持久化失败可忽略，仅影响本次会话。
      }
      return next;
    });
  }, [storageKey]);

  const resetColumns = useCallback(() => {
    setHiddenColumns(defaultHidden);
    try {
      localStorage.setItem(storageKey, JSON.stringify(defaultHidden));
    } catch {
      // 忽略持久化失败。
    }
  }, [defaultHidden, storageKey]);

  const visibleColumns = useMemo<ReadonlyArray<T>>(
    () => columns.filter((column) => column.fixed || !hiddenColumns.includes(column.key)),
    [columns, hiddenColumns],
  );

  return { hiddenColumns, visibleColumns, toggleColumn, resetColumns };
}

export function ColumnMenu({
  columns,
  hiddenColumns,
  onToggle,
  onReset,
}: {
  columns: ReadonlyArray<ConsoleColumnDefinition>;
  hiddenColumns: string[];
  onToggle: (key: string) => void;
  onReset: () => void;
}) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return undefined;
    const handlePointerDown = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [open]);

  return (
    <div className="console-col-wrap" ref={menuRef}>
      <button
        type="button"
        className="console-icon-button"
        aria-label="列显示设置"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <Columns3 size={16} />
      </button>
      {open && (
        <div className="console-col-menu" role="group" aria-label="表格列显示">
          <p className="console-col-menu-title">显示列</p>
          {columns.map((column) => (
            <label key={column.key} className="console-col-menu-row">
              <input
                type="checkbox"
                checked={column.fixed || !hiddenColumns.includes(column.key)}
                disabled={column.fixed}
                onChange={() => onToggle(column.key)}
              />
              {column.label}
            </label>
          ))}
          <div className="console-col-menu-actions">
            <button type="button" className="console-secondary-button" onClick={onReset}>恢复默认</button>
          </div>
        </div>
      )}
    </div>
  );
}
