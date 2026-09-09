import type { ReactNode } from 'react';
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react';
import type { MetadataSortOrder } from '../../api/consoleTypes';
import { cn } from '../../lib/cn';
import { DEFAULT_PAGE_SIZE, PaginationBar } from '../console/ConsoleTableControls';

export interface MetadataSortState {
  orderBy: string | null;
  order: MetadataSortOrder;
}

export const DEFAULT_SORT_STATE: MetadataSortState = { orderBy: null, order: 'desc' };

/** 未排序 → desc → asc → 清除（回到服务端默认排序）。 */
export function nextSortState(current: MetadataSortState, field: string): MetadataSortState {
  if (current.orderBy !== field) return { orderBy: field, order: 'desc' };
  if (current.order === 'desc') return { orderBy: field, order: 'asc' };
  return DEFAULT_SORT_STATE;
}

export function SortableTh({
  label,
  field,
  sort,
  onSort,
}: {
  label: string;
  field: string;
  sort: MetadataSortState;
  onSort: (field: string) => void;
}) {
  const active = sort.orderBy === field;
  const Icon = !active ? ArrowUpDown : sort.order === 'asc' ? ArrowUp : ArrowDown;
  return (
    <th aria-sort={active ? (sort.order === 'asc' ? 'ascending' : 'descending') : 'none'}>
      <button
        type="button"
        className={cn('console-th-btn', active && 'is-active')}
        onClick={() => onSort(field)}
      >
        {label}
        <Icon size={12} aria-hidden="true" />
      </button>
    </th>
  );
}

export interface MetadataActiveFilter {
  key: string;
  label: string;
  value: string;
  onClear: () => void;
}

export function MetadataToolbar({
  onApply,
  onReset,
  activeFilters,
  totalCount,
  totalLabel,
  extra,
  children,
}: {
  onApply?: () => void;
  onReset: () => void;
  activeFilters: MetadataActiveFilter[];
  totalCount: number | null;
  totalLabel: string;
  extra?: ReactNode;
  children: ReactNode;
}) {
  const controls = (
    <>
      {children}
      {onApply && <button type="submit" className="console-secondary-button">筛选</button>}
      <button type="button" className="console-secondary-button" onClick={onReset}>重置</button>
    </>
  );
  return (
    <div className="console-toolbar">
      {onApply ? (
        <form onSubmit={(event) => { event.preventDefault(); onApply(); }}>{controls}</form>
      ) : (
        <div className="console-toolbar-controls">{controls}</div>
      )}
      <span className="console-toolbar-spacer" aria-hidden="true" />
      {extra}
      <div className="console-toolbar-status">
        {activeFilters.map((filter) => (
          <span key={filter.key} className="console-filter-chip">
            {filter.label} {filter.value}
            <button type="button" aria-label={`清除${filter.label}筛选`} onClick={filter.onClear}>×</button>
          </span>
        ))}
        <span className="console-chip">
          {totalCount != null ? `${totalLabel} ${totalCount}` : '数据来源 · Gateway 元数据'}
        </span>
      </div>
    </div>
  );
}

function ToolbarSelect({
  id,
  label,
  value,
  options,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  options: readonly string[];
  onChange: (value: string) => void;
}) {
  return (
    <>
      <label htmlFor={id}>{label}</label>
      <select id={id} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">全部</option>
        {options.map((option) => (
          <option key={option} value={option}>{option}</option>
        ))}
      </select>
    </>
  );
}

export { ToolbarSelect };

export interface MetadataSummaryChip {
  key: string;
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}

export function MetadataSummaryStrip({
  chips,
  testId,
}: {
  chips: MetadataSummaryChip[];
  testId: string;
}) {
  return (
    <div className="console-summary" data-testid={testId}>
      {chips.map((chip) => (
        <button
          key={chip.key}
          type="button"
          className={cn('console-chip console-summary-chip', chip.active && 'is-active')}
          onClick={chip.onClick}
        >
          {chip.label} {chip.count}
        </button>
      ))}
    </div>
  );
}

export function MetadataPagination({
  offset,
  itemCount,
  hasMore,
  onPrev,
  onNext,
  pageSize = DEFAULT_PAGE_SIZE,
  onPageSizeChange,
}: {
  offset: number;
  itemCount: number;
  hasMore: boolean;
  onPrev: () => void;
  onNext: () => void;
  pageSize?: number;
  onPageSizeChange?: (pageSize: number) => void;
}) {
  const pageNumber = Math.floor(Math.max(0, offset) / Math.max(1, pageSize)) + 1;
  return (
    <PaginationBar
      page={pageNumber}
      itemCount={itemCount}
      hasMore={hasMore}
      pageSize={pageSize}
      onPageSizeChange={onPageSizeChange}
      onPrevious={onPrev}
      onNext={onNext}
    />
  );
}
