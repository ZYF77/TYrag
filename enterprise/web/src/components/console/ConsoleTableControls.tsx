import { ChevronLeft, ChevronRight } from 'lucide-react';

export const DEFAULT_PAGE_SIZE = 20;
export const PAGE_SIZE_OPTIONS = [20, 50, 100] as const;

interface PaginationBarProps {
  page: number;
  itemCount: number;
  hasMore?: boolean;
  pageSize: number;
  onPageSizeChange?: (pageSize: number) => void;
  onPrevious: () => void;
  onNext: () => void;
  total?: number | null;
  label?: string;
}

export function PaginationBar({
  page,
  itemCount,
  hasMore = false,
  pageSize,
  onPageSizeChange,
  onPrevious,
  onNext,
  total,
  label = '条',
}: PaginationBarProps) {
  return (
    <nav className="console-pagination console-pagination--fixed" aria-label="分页">
      {onPageSizeChange && (
        <label className="console-page-size">
          <select aria-label="每页大小" value={pageSize} onChange={(event) => onPageSizeChange(Number(event.target.value))}>
            {PAGE_SIZE_OPTIONS.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
      )}
      <button type="button" className="console-secondary-button" disabled={page <= 1} onClick={onPrevious}>
        <ChevronLeft size={14} aria-hidden="true" />
        上一页
      </button>
      <span aria-live="polite">
        第 {page} 页 · 本页 {itemCount} {label}
        {total != null ? ` · 共 ${total} ${label}` : ''}
      </span>
      <button type="button" className="console-secondary-button" disabled={!hasMore} onClick={onNext}>
        下一页
        <ChevronRight size={14} aria-hidden="true" />
      </button>
    </nav>
  );
}
