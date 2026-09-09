import type { ReactNode } from 'react';
import { SortableTh, type MetadataSortState } from './MetadataControls';

export interface ConsoleDataTableColumn<T> {
  key: string;
  label: string;
  /** 提供且调用方传入 sort/onSort 时渲染为可排序表头。 */
  sortField?: string | null;
  thClassName?: string;
  render: (item: T) => ReactNode;
}

/**
 * Console 数据表骨架：SortableTh 表头循环 + 可点击行（data-row-action +
 * tabIndex + Enter/Space 键盘触发）+ 空态 + 分页插槽。
 * items 为空时优先渲染 empty（表格整体让位给空态文案），
 * 否则若提供 emptyRow 则在 tbody 内渲染空行（如 RAG 诊断表）。
 */
export function ConsoleDataTable<T>({
  columns,
  items,
  rowKey,
  onRowAction,
  sort,
  onSort,
  testId,
  empty,
  emptyRow,
  pagination,
}: {
  columns: ReadonlyArray<ConsoleDataTableColumn<T>>;
  items: readonly T[];
  rowKey: (item: T) => string;
  /** 行点击 / Enter / Space 触发的行级动作；缺省时行不可交互。 */
  onRowAction?: (item: T) => void;
  sort?: MetadataSortState;
  onSort?: (field: string) => void;
  testId?: string;
  /** items 为空时渲染的空态（替代整个表格）。 */
  empty?: ReactNode;
  /** items 为空且未提供 empty 时，tbody 内渲染的空行（表格仍渲染）。 */
  emptyRow?: ReactNode;
  /** 表格（或空态）之后渲染的分页。 */
  pagination?: ReactNode;
}) {
  const sortable = Boolean(sort && onSort);
  return (
    <>
      {items.length > 0 || (!empty && emptyRow) ? (
        <div className="console-table-wrap">
          <table className="console-table" data-testid={testId}>
            <thead>
              <tr>
                {columns.map((column) => (
                  sortable && column.sortField
                    ? <SortableTh key={column.key} label={column.label} field={column.sortField} sort={sort!} onSort={onSort!} />
                    : <th key={column.key} className={column.thClassName}>{column.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr
                  key={rowKey(item)}
                  data-row-action={onRowAction ? 'true' : undefined}
                  tabIndex={onRowAction ? 0 : undefined}
                  onClick={onRowAction ? () => onRowAction(item) : undefined}
                  onKeyDown={onRowAction
                    ? (event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        onRowAction(item);
                      }
                    }
                    : undefined}
                >
                  {columns.map((column) => column.render(item))}
                </tr>
              ))}
              {items.length === 0 && emptyRow}
            </tbody>
          </table>
        </div>
      ) : (
        empty
      )}
      {pagination}
    </>
  );
}
