import { useCallback, useEffect, useState } from 'react';
import { toDisplayError, v2Api } from '../api/v2Client';
import type { ConsoleState, MetadataSummary } from '../api/consoleTypes';
import { DEFAULT_PAGE_SIZE } from '../components/console/ConsoleTableControls';
import { initialPanelState, panelErrorStatus } from '../components/common/Panel';
import {
  DEFAULT_SORT_STATE,
  nextSortState,
  type MetadataSortState,
} from '../components/common/MetadataControls';

export interface MetadataPanelQuery {
  limit: number;
  offset: number;
  sort: MetadataSortState;
}

export interface MetadataPanelStateOptions<TPage, TFilters extends Record<string, string>> {
  /** 拉取一页元数据；面板私有的即时筛选（状态/来源系统等）由调用方闭包捕获。 */
  fetchPage: (query: MetadataPanelQuery, advancedFilters: TFilters) => Promise<TPage>;
  emptyFilters: TFilters;
  /** 应用高级检索前对草稿做 trim 归一。 */
  normaliseFilters?: (draft: TFilters) => TFilters;
}

/**
 * Console 元数据面板的共享状态机（会话元数据 / 文件元数据 / 会话管理）：
 * 页状态、offset/pageSize、requestToken、排序三态循环、汇总 chips 数据、
 * 高级检索草稿与生效值。触发语义与原三份手写实现一致：
 * 每次交互恰好一个列表请求；排序 none → desc → asc → none；
 * 汇总失败静默降级，不阻断主表。
 */
export function useMetadataPanelState<TPage, TFilters extends Record<string, string>>(
  options: MetadataPanelStateOptions<TPage, TFilters>,
) {
  const { fetchPage, emptyFilters, normaliseFilters } = options;
  const [state, setState] = useState<ConsoleState<TPage>>(initialPanelState<TPage>);
  const [sort, setSort] = useState<MetadataSortState>(DEFAULT_SORT_STATE);
  const [offset, setOffset] = useState(0);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [requestToken, setRequestToken] = useState(0);
  const [summary, setSummary] = useState<MetadataSummary | null>(null);
  const [advancedDraft, setAdvancedDraft] = useState<TFilters>(emptyFilters);
  const [advancedFilters, setAdvancedFilters] = useState<TFilters>(emptyFilters);

  const reload = useCallback(() => setRequestToken((token) => token + 1), []);

  const load = useCallback(async () => {
    setState(initialPanelState<TPage>());
    try {
      const data = await fetchPage({ limit: pageSize, offset, sort }, advancedFilters);
      setState({ status: 'healthy', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setState({ status: panelErrorStatus(displayError), data: null, error: displayError });
    }
  }, [advancedFilters, fetchPage, offset, pageSize, sort]);

  const loadSummary = useCallback(async () => {
    try {
      setSummary(await v2Api.getMetadataSummary());
    } catch {
      // 汇总失败静默降级，不阻断主表。
      setSummary(null);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, requestToken]);

  useEffect(() => {
    void loadSummary();
  }, [loadSummary]);

  const handleSort = useCallback((field: string) => {
    setSort((current) => nextSortState(current, field));
    setOffset(0);
  }, []);

  const applyAdvancedFilters = useCallback(() => {
    setAdvancedFilters(normaliseFilters ? normaliseFilters(advancedDraft) : advancedDraft);
    setOffset(0);
  }, [advancedDraft, normaliseFilters]);

  const clearAdvancedFilter = useCallback((key: keyof TFilters & string) => {
    setAdvancedDraft((current) => ({ ...current, [key]: '' }));
    setAdvancedFilters((current) => ({ ...current, [key]: '' }));
    setOffset(0);
  }, []);

  /** 高级筛选草稿/生效值 + 排序 + 页码一起复位（不含面板私有筛选）。 */
  const resetAdvancedAndSort = useCallback(() => {
    setAdvancedDraft(emptyFilters);
    setAdvancedFilters(emptyFilters);
    setSort(DEFAULT_SORT_STATE);
    setOffset(0);
  }, [emptyFilters]);

  return {
    state,
    summary,
    sort,
    offset,
    pageSize,
    advancedDraft,
    advancedFilters,
    setAdvancedDraft,
    setAdvancedFilters,
    setOffset,
    setPageSize,
    reload,
    loadSummary,
    handleSort,
    applyAdvancedFilters,
    clearAdvancedFilter,
    resetAdvancedAndSort,
  };
}
