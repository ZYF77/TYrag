import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, ArrowUpDown, Columns3, RefreshCw } from 'lucide-react';
import { toDisplayError, v2Api } from '../../api/v2Client';
import type {
  CallbackEndpointConfig,
  ConsoleModuleStatus,
  ConsoleState,
  ConversationMetadataItem,
  ConversationMetadataOrderBy,
  ConversationMetadataPage,
  DocumentMetadataItem,
  DocumentMetadataOrderBy,
  DocumentMetadataPage,
  EamProbeResult,
  MetadataSortOrder,
  MetadataSummary,
  SystemIntegrations,
} from '../../api/consoleTypes';
import type { DisplayError } from '../../api/v2Types';

const PAGE_LIMIT = 50;
const NOT_PROVIDED = '未提供';

const CONVERSATION_STATUS_OPTIONS = ['active', 'archived'] as const;
// 常见同步状态，对齐 enterprise/gateway/sync/status_mapping.py 的 stage 枚举。
const SYNC_STATUS_OPTIONS = [
  'ready',
  'failed',
  'parsing',
  'registered',
  'cancelled',
  'queued',
  'indexing',
  'superseded',
] as const;
const BUSINESS_STATUS_OPTIONS = ['active', 'review_required'] as const;

const DOC_HIDDEN_COLUMNS_KEY = 'console.docMeta.hiddenColumns';
const DOC_DEFAULT_HIDDEN_COLUMNS = ['sourceSize', 'createdAt'];

interface ProbeState {
  phase: 'idle' | 'probing' | 'connected' | 'failed';
  result?: EamProbeResult;
  error?: DisplayError;
}

function initialPanelState<T>(): ConsoleState<T> {
  return { status: 'processing', data: null, error: null };
}

export function panelErrorStatus(error: DisplayError): ConsoleModuleStatus {
  if (error.httpStatus === 401 || error.httpStatus === 403) return 'unauthorized';
  if (error.httpStatus === 0 || error.httpStatus === 502 || error.httpStatus === 503) {
    return 'unavailable';
  }
  return 'failed';
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return NOT_PROVIDED;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

function formatValue(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return NOT_PROVIDED;
  return String(value);
}

function PanelBadge({ status }: { status: ConsoleModuleStatus }) {
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
}: {
  eyebrow: string;
  title: string;
  description: string;
  status: ConsoleModuleStatus;
  actions?: React.ReactNode;
  children: React.ReactNode;
  testId: string;
}) {
  return (
    <section data-testid={testId} className="console-card">
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

export function PanelError({ error, onRetry }: { error: DisplayError; onRetry: () => void }) {
  return (
    <div role="alert" className="console-alert">
      <p><strong>{error.code}</strong>{error.httpStatus ? ` · HTTP ${error.httpStatus}` : ''} · {error.message}</p>
      <button type="button" onClick={onRetry} className="console-secondary-button">重试</button>
    </div>
  );
}

function ProbeBadge({ state }: { state: ProbeState }) {
  if (state.phase === 'probing') {
    return (
      <span className="console-status console-status--processing">
        <span className="console-status-dot" aria-hidden="true" />
        检测中
      </span>
    );
  }
  if (state.phase === 'connected') {
    return (
      <span className="console-status console-status--connected">
        <span className="console-status-dot" aria-hidden="true" />
        connected
        {state.result?.httpStatus != null ? ` · HTTP ${state.result.httpStatus}` : ''}
        {state.result?.latencyMs != null ? ` · ${state.result.latencyMs}ms` : ''}
      </span>
    );
  }
  if (state.phase === 'failed') {
    const detail = state.result?.errorCode ?? state.error?.code;
    return (
      <span className="console-status console-status--failed">
        <span className="console-status-dot" aria-hidden="true" />
        failed
        {state.result?.httpStatus != null ? ` · HTTP ${state.result.httpStatus}` : ''}
        {detail ? ` · ${detail}` : ''}
      </span>
    );
  }
  return null;
}

export function IntegrationsPanel() {
  const [state, setState] = useState<ConsoleState<SystemIntegrations>>(initialPanelState<SystemIntegrations>);
  const [probes, setProbes] = useState<Record<string, ProbeState>>({});

  const load = useCallback(async () => {
    setState(initialPanelState<SystemIntegrations>());
    try {
      const data = await v2Api.getSystemIntegrations();
      setState({ status: 'healthy', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setState({ status: panelErrorStatus(displayError), data: null, error: displayError });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const runProbe = useCallback(async (callback: CallbackEndpointConfig) => {
    setProbes((current) => ({ ...current, [callback.binding]: { phase: 'probing' } }));
    try {
      const result = await v2Api.probeEamCallback(callback.binding);
      setProbes((current) => ({
        ...current,
        [callback.binding]: { phase: result.status === 'connected' ? 'connected' : 'failed', result },
      }));
    } catch (error) {
      const displayError = toDisplayError(error);
      setProbes((current) => ({ ...current, [callback.binding]: { phase: 'failed', error: displayError } }));
    }
  }, []);

  const data = state.data;

  return (
    <div className="console-grid">
      <PanelCard
        eyebrow="RAGFlow"
        title="RAGFlow 接口配置"
        description="当前 Gateway 指向的 RAGFlow 地址与主要 API 路径，只读展示。"
        status={state.status}
        actions={(
          <button
            type="button"
            onClick={() => void load()}
            className="console-icon-button"
            aria-label="刷新接口配置"
          >
            <RefreshCw size={16} />
          </button>
        )}
        testId="console-integrations-card"
      >
        {data && (
          <>
            <div className="console-row">
              <div>
                <p>Base URL</p>
                <p className="console-route">{data.ragflow.baseUrl}</p>
              </div>
              <span className="console-chip">apiVersion · {data.ragflow.apiVersion}</span>
            </div>
            {Object.entries(data.ragflow.paths).map(([name, path]) => (
              <div className="console-row" key={name}>
                <div>
                  <p>{name}</p>
                  <p className="console-route">{path}</p>
                </div>
              </div>
            ))}
          </>
        )}
        {state.error && <PanelError error={state.error} onRetry={() => void load()} />}
      </PanelCard>
      <PanelCard
        eyebrow="Callbacks"
        title="回调接口配置"
        description="已注册的回调接口；检测联通只发起一次探测请求，不展示凭据内容。"
        status={state.status}
        testId="console-callbacks-card"
      >
        {data && !data.callbacksEnabled && <p className="console-hint">回调功能当前未启用。</p>}
        {data?.callbacks.length ? (
          <div className="console-table-wrap">
            <table className="console-table" data-testid="console-callbacks-table">
              <thead>
                <tr>
                  <th>Binding</th>
                  <th>来源系统</th>
                  <th>租户</th>
                  <th>方法</th>
                  <th>Base URL</th>
                  <th>路径</th>
                  <th>启用</th>
                  <th>凭据</th>
                  <th className="console-col-center">联通检测</th>
                </tr>
              </thead>
              <tbody>
                {data.callbacks.map((callback) => {
                  const probe: ProbeState = probes[callback.binding] ?? { phase: 'idle' };
                  return (
                    <tr key={`${callback.binding}-${callback.tenantId ?? 'all'}`}>
                      <td>{callback.binding}</td>
                      <td>{callback.sourceSystem}</td>
                      <td>{callback.tenantId ?? '全部'}</td>
                      <td>{callback.method}</td>
                      <td className="console-table-mono">{callback.baseUrl}</td>
                      <td className="console-table-mono">{callback.path}</td>
                      <td>{callback.enabled ? '启用' : '停用'}</td>
                      <td>{callback.credentialConfigured ? '已配置' : '未配置'}</td>
                      <td>
                        <div className="console-probe-cell">
                          <button
                            type="button"
                            className="console-secondary-button"
                            disabled={probe.phase === 'probing'}
                            onClick={() => void runProbe(callback)}
                          >
                            检测联通
                          </button>
                          <ProbeBadge state={probe} />
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          data && <p className="console-empty">暂无回调配置。</p>
        )}
        {!data && !state.error && <p className="console-hint">接口配置加载中…</p>}
      </PanelCard>
    </div>
  );
}

// ---- Shared metadata building blocks (toolbars, summary, sort, pills) ------

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

/** 业务状态色板：绿=正常、红=失败、橙=需关注、蓝=处理中、灰=其他。 */
const STATUS_PILL_TONES: Record<string, string> = {
  ready: 'ok',
  active: 'ok',
  completed: 'ok',
  failed: 'failed',
  review_required: 'warn',
  no_reliable_evidence: 'warn',
  parsing: 'processing',
  processing: 'processing',
  running: 'processing',
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
        className={`console-th-btn${active ? ' is-active' : ''}`}
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
  extra?: React.ReactNode;
  children: React.ReactNode;
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
          className={`console-chip console-summary-chip${chip.active ? ' is-active' : ''}`}
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
}: {
  offset: number;
  itemCount: number;
  hasMore: boolean;
  onPrev: () => void;
  onNext: () => void;
}) {
  return (
    <div className="console-pagination">
      <button type="button" className="console-secondary-button" disabled={offset <= 0} onClick={onPrev}>
        上一页
      </button>
      <span>offset {offset} · 本页 {itemCount} 条</span>
      <button type="button" className="console-secondary-button" disabled={!hasMore} onClick={onNext}>
        下一页
      </button>
    </div>
  );
}

// ---- 会话元数据 ------------------------------------------------------------

type ConversationColumnKey =
  | 'conversationId'
  | 'businessUserId'
  | 'equipmentId'
  | 'fixedAssetNo'
  | 'status'
  | 'contextVersion'
  | 'ragflow'
  | 'createdAt'
  | 'lastMessageAt';

const CONVERSATION_COLUMNS: Array<{ key: ConversationColumnKey; label: string; sortField: ConversationMetadataOrderBy | null; fixed?: boolean }> = [
  { key: 'conversationId', label: '会话', sortField: 'conversationId', fixed: true },
  { key: 'businessUserId', label: '业务用户', sortField: 'businessUserId' },
  { key: 'equipmentId', label: '设备', sortField: 'equipmentId' },
  { key: 'fixedAssetNo', label: '固定资产', sortField: 'fixedAssetNo' },
  { key: 'status', label: '状态', sortField: 'status' },
  { key: 'contextVersion', label: 'Context', sortField: 'contextVersion' },
  { key: 'ragflow', label: 'RAGFlow', sortField: null },
  { key: 'createdAt', label: '创建时间', sortField: 'createdAt' },
  { key: 'lastMessageAt', label: '最近消息', sortField: 'lastMessageAt' },
];

const CONVERSATION_HIDDEN_COLUMNS_KEY = 'console.convMeta.hiddenColumns';
const CONVERSATION_DEFAULT_HIDDEN_COLUMNS: string[] = [];

function renderConversationCell(item: ConversationMetadataItem, key: ConversationColumnKey): React.ReactNode {
  switch (key) {
    case 'conversationId':
      return <td key={key} className="console-table-mono">{item.conversationId}</td>;
    case 'businessUserId':
      return <td key={key}>{item.businessUserId}</td>;
    case 'equipmentId':
      return <td key={key}>{formatValue(item.equipmentId)}</td>;
    case 'fixedAssetNo':
      return <td key={key}>{formatValue(item.fixedAssetNo)}</td>;
    case 'status':
      return <td key={key}><StatusPill code={item.status} /></td>;
    case 'contextVersion':
      return <td key={key}>v{item.contextVersion}</td>;
    case 'ragflow':
      return <td key={key} className="console-table-mono">{item.ragflowChatId ?? item.ragflowSessionId ?? NOT_PROVIDED}</td>;
    case 'createdAt':
      return <td key={key}>{formatTime(item.createdAt)}</td>;
    case 'lastMessageAt':
      return <td key={key}>{formatTime(item.lastMessageAt)}</td>;
    default:
      return null;
  }
}

export function ConversationMetadataPanel() {
  const [state, setState] = useState<ConsoleState<ConversationMetadataPage>>(initialPanelState<ConversationMetadataPage>);
  const [statusFilter, setStatusFilter] = useState('');
  const [sort, setSort] = useState<MetadataSortState>(DEFAULT_SORT_STATE);
  const [offset, setOffset] = useState(0);
  const [requestToken, setRequestToken] = useState(0);
  const [summary, setSummary] = useState<MetadataSummary | null>(null);
  const { hiddenColumns: hiddenConversationColumns, visibleColumns: visibleConversationColumns, toggleColumn: toggleConversationColumn, resetColumns: resetConversationColumns } = useHiddenTableColumns(
    CONVERSATION_HIDDEN_COLUMNS_KEY,
    CONVERSATION_DEFAULT_HIDDEN_COLUMNS,
    CONVERSATION_COLUMNS,
  );

  const reload = useCallback(() => setRequestToken((token) => token + 1), []);

  const load = useCallback(async () => {
    setState(initialPanelState<ConversationMetadataPage>());
    try {
      const data = await v2Api.listAdminConversationMetadata({
        limit: PAGE_LIMIT,
        offset,
        status: statusFilter || null,
        orderBy: sort.orderBy as ConversationMetadataOrderBy | null,
        order: sort.orderBy ? sort.order : null,
      });
      setState({ status: 'healthy', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setState({ status: panelErrorStatus(displayError), data: null, error: displayError });
    }
  }, [offset, sort, statusFilter]);

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

  const applyFilters = useCallback(() => {
    setOffset(0);
    reload();
  }, [reload]);

  const resetFilters = useCallback(() => {
    setStatusFilter('');
    setSort(DEFAULT_SORT_STATE);
    setOffset(0);
  }, []);

  const handleSort = useCallback((field: string) => {
    setSort((current) => nextSortState(current, field));
    setOffset(0);
  }, []);

  const activeFilters = useMemo<MetadataActiveFilter[]>(() => (
    statusFilter
      ? [{ key: 'status', label: '状态', value: statusFilter, onClear: () => { setStatusFilter(''); setOffset(0); } }]
      : []
  ), [statusFilter]);

  const summaryChips = useMemo<MetadataSummaryChip[]>(() => {
    const byStatus = summary?.conversations.byStatus ?? {};
    const chips: MetadataSummaryChip[] = [
      {
        key: 'total',
        label: '会话',
        count: summary?.conversations.total ?? 0,
        active: !statusFilter,
        onClick: () => { setStatusFilter(''); setOffset(0); },
      },
    ];
    for (const [status, count] of Object.entries(byStatus)) {
      chips.push({
        key: `status-${status}`,
        label: status,
        count,
        active: statusFilter === status,
        onClick: () => { setStatusFilter(statusFilter === status ? '' : status); setOffset(0); },
      });
    }
    return chips;
  }, [statusFilter, summary]);

  const page = state.data;

  return (
    <PanelCard
      eyebrow="Metadata"
      title="会话元数据"
      description="管理员视角的租户级会话元数据；只读展示，不回显消息正文。"
      status={state.status}
      actions={(
        <button
          type="button"
          onClick={() => { reload(); void loadSummary(); }}
          className="console-icon-button"
          aria-label="刷新会话元数据"
        >
          <RefreshCw size={16} />
        </button>
      )}
      testId="console-meta-conversations-card"
    >
      <MetadataToolbar
        onApply={applyFilters}
        onReset={resetFilters}
        activeFilters={activeFilters}
        totalCount={summary?.conversations.total ?? null}
        totalLabel="会话"
        extra={(
          <ColumnMenu
            columns={CONVERSATION_COLUMNS}
            hiddenColumns={hiddenConversationColumns}
            onToggle={toggleConversationColumn}
            onReset={resetConversationColumns}
          />
        )}
      >
        <ToolbarSelect
          id="conversation-status-filter"
          label="状态"
          value={statusFilter}
          options={CONVERSATION_STATUS_OPTIONS}
          onChange={(value) => { setStatusFilter(value); setOffset(0); }}
        />
      </MetadataToolbar>
      {summary && (
        <MetadataSummaryStrip chips={summaryChips} testId="console-conversations-summary" />
      )}
      {page?.items.length ? (
        <div className="console-table-wrap">
          <table className="console-table" data-testid="console-meta-conversations-table">
            <thead>
              <tr>
                {visibleConversationColumns.map((column) => (
                  column.sortField
                    ? <SortableTh key={column.key} label={column.label} field={column.sortField} sort={sort} onSort={handleSort} />
                    : <th key={column.key}>{column.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {page.items.map((item) => (
                <tr key={item.conversationId}>
                  {visibleConversationColumns.map((column) => renderConversationCell(item, column.key))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="console-empty">
          {state.status === 'processing' ? '会话元数据加载中…' : '暂无会话元数据。'}
        </p>
      )}
      <MetadataPagination
        offset={offset}
        itemCount={page?.items.length ?? 0}
        hasMore={Boolean(page?.hasMore)}
        onPrev={() => setOffset(Math.max(0, offset - PAGE_LIMIT))}
        onNext={() => setOffset(offset + PAGE_LIMIT)}
      />
      {state.error && <PanelError error={state.error} onRetry={reload} />}
    </PanelCard>
  );
}

// ---- 文件元数据 ------------------------------------------------------------

type DocColumnKey =
  | 'externalDocumentId'
  | 'fileName'
  | 'sourceSystem'
  | 'documentType'
  | 'equipmentId'
  | 'fixedAssetNo'
  | 'assetId'
  | 'syncStatus'
  | 'businessStatus'
  | 'ragflow'
  | 'sourceSize'
  | 'createdAt'
  | 'updatedAt'
  | 'parsedAt'
  | 'eamNotifiedAt';

const DOC_COLUMNS: Array<{ key: DocColumnKey; label: string; sortField: DocumentMetadataOrderBy | null; fixed?: boolean }> = [
  { key: 'externalDocumentId', label: '文档', sortField: 'externalDocumentId', fixed: true },
  { key: 'fileName', label: '文件名', sortField: 'fileName' },
  { key: 'sourceSystem', label: '来源系统', sortField: 'sourceSystem' },
  { key: 'documentType', label: '类型', sortField: 'documentType' },
  { key: 'equipmentId', label: '设备', sortField: 'equipmentId' },
  { key: 'fixedAssetNo', label: '固定资产', sortField: 'fixedAssetNo' },
  { key: 'assetId', label: '资产', sortField: 'assetId' },
  { key: 'syncStatus', label: '同步状态', sortField: 'syncStatus' },
  { key: 'businessStatus', label: '业务状态', sortField: 'businessStatus' },
  { key: 'ragflow', label: 'RAGFlow', sortField: null },
  { key: 'sourceSize', label: '大小', sortField: 'sourceSize' },
  { key: 'createdAt', label: '创建时间', sortField: 'createdAt' },
  { key: 'updatedAt', label: '更新时间', sortField: 'updatedAt' },
  { key: 'parsedAt', label: 'RAGFlow解析完成', sortField: 'parsedAt' },
  { key: 'eamNotifiedAt', label: 'EAM通知时间', sortField: 'eamNotifiedAt' },
];

function renderDocumentCell(item: DocumentMetadataItem, key: DocColumnKey): React.ReactNode {
  switch (key) {
    case 'externalDocumentId':
      return <td key={key} className="console-table-mono">{item.externalDocumentId}</td>;
    case 'fileName':
      return <td key={key}>{item.fileName}</td>;
    case 'sourceSystem':
      return <td key={key}>{item.sourceSystem}</td>;
    case 'documentType':
      return <td key={key}>{formatValue(item.documentType)}</td>;
    case 'equipmentId':
      return <td key={key}>{formatValue(item.equipmentId)}</td>;
    case 'fixedAssetNo':
      return <td key={key}>{formatValue(item.fixedAssetNo)}</td>;
    case 'assetId':
      return <td key={key}>{formatValue(item.assetId)}</td>;
    case 'syncStatus':
      return <td key={key}><StatusPill code={item.syncStatus} /></td>;
    case 'businessStatus':
      return <td key={key}><StatusPill code={item.businessStatus} /></td>;
    case 'ragflow':
      return (
        <td key={key} className="console-table-mono">
          {item.ragflowDatasetId || item.ragflowDocumentId
            ? `dataset ${item.ragflowDatasetId ?? '-'} / doc ${item.ragflowDocumentId ?? '-'}`
            : NOT_PROVIDED}
        </td>
      );
    case 'sourceSize':
      return <td key={key}>{formatValue(item.sourceSize)}</td>;
    case 'createdAt':
      return <td key={key}>{formatTime(item.createdAt)}</td>;
    case 'updatedAt':
      return <td key={key}>{formatTime(item.updatedAt)}</td>;
    case 'parsedAt':
      return <td key={key}>{formatTime(item.parsedAt)}</td>;
    case 'eamNotifiedAt':
      return <td key={key}>{formatTime(item.eamNotifiedAt)}</td>;
    default:
      return null;
  }
}

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

export function DocumentMetadataPanel() {
  const [state, setState] = useState<ConsoleState<DocumentMetadataPage>>(initialPanelState<DocumentMetadataPage>);
  const [sourceDraft, setSourceDraft] = useState('');
  const [sourceFilter, setSourceFilter] = useState('');
  const [syncStatusFilter, setSyncStatusFilter] = useState('');
  const [businessStatusFilter, setBusinessStatusFilter] = useState('');
  const [sort, setSort] = useState<MetadataSortState>(DEFAULT_SORT_STATE);
  const { hiddenColumns, visibleColumns: visibleDocColumns, toggleColumn: toggleDocColumn, resetColumns: resetDocColumns } = useHiddenTableColumns(
    DOC_HIDDEN_COLUMNS_KEY,
    DOC_DEFAULT_HIDDEN_COLUMNS,
    DOC_COLUMNS,
  );
  const [offset, setOffset] = useState(0);
  const [requestToken, setRequestToken] = useState(0);
  const [summary, setSummary] = useState<MetadataSummary | null>(null);

  const reload = useCallback(() => setRequestToken((token) => token + 1), []);

  const load = useCallback(async () => {
    setState(initialPanelState<DocumentMetadataPage>());
    try {
      const data = await v2Api.listAdminDocumentMetadata({
        limit: PAGE_LIMIT,
        offset,
        sourceSystem: sourceFilter || null,
        status: syncStatusFilter || null,
        businessStatus: businessStatusFilter || null,
        orderBy: sort.orderBy as DocumentMetadataOrderBy | null,
        order: sort.orderBy ? sort.order : null,
      });
      setState({ status: 'healthy', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setState({ status: panelErrorStatus(displayError), data: null, error: displayError });
    }
  }, [businessStatusFilter, offset, sort, sourceFilter, syncStatusFilter]);

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

  const applyFilters = useCallback(() => {
    setSourceFilter(sourceDraft.trim());
    setOffset(0);
  }, [sourceDraft]);

  const clearAllFilters = useCallback(() => {
    setSourceDraft('');
    setSourceFilter('');
    setSyncStatusFilter('');
    setBusinessStatusFilter('');
    setOffset(0);
  }, []);

  const resetFilters = useCallback(() => {
    clearAllFilters();
    setSort(DEFAULT_SORT_STATE);
  }, [clearAllFilters]);

  const handleSort = useCallback((field: string) => {
    setSort((current) => nextSortState(current, field));
    setOffset(0);
  }, []);

  const activeFilters = useMemo<MetadataActiveFilter[]>(() => {
    const filters: MetadataActiveFilter[] = [];
    if (sourceFilter) {
      filters.push({ key: 'sourceSystem', label: '来源系统', value: sourceFilter, onClear: () => { setSourceFilter(''); setOffset(0); } });
    }
    if (syncStatusFilter) {
      filters.push({ key: 'syncStatus', label: '同步状态', value: syncStatusFilter, onClear: () => { setSyncStatusFilter(''); setOffset(0); } });
    }
    if (businessStatusFilter) {
      filters.push({ key: 'businessStatus', label: '业务状态', value: businessStatusFilter, onClear: () => { setBusinessStatusFilter(''); setOffset(0); } });
    }
    return filters;
  }, [businessStatusFilter, sourceFilter, syncStatusFilter]);

  const summaryChips = useMemo<MetadataSummaryChip[]>(() => {
    const bySync = summary?.documents.bySyncStatus ?? {};
    const byBusiness = summary?.documents.byBusinessStatus ?? {};
    const chips: MetadataSummaryChip[] = [
      {
        key: 'total',
        label: '文档',
        count: summary?.documents.total ?? 0,
        active: !sourceFilter && !syncStatusFilter && !businessStatusFilter,
        onClick: clearAllFilters,
      },
    ];
    for (const [status, count] of Object.entries(bySync)) {
      chips.push({
        key: `sync-${status}`,
        label: status,
        count,
        active: syncStatusFilter === status,
        onClick: () => { setSyncStatusFilter(syncStatusFilter === status ? '' : status); setOffset(0); },
      });
    }
    const reviewRequired = byBusiness.review_required;
    if (reviewRequired && reviewRequired > 0) {
      chips.push({
        key: 'business-review_required',
        label: 'review_required',
        count: reviewRequired,
        active: businessStatusFilter === 'review_required',
        onClick: () => {
          setBusinessStatusFilter(businessStatusFilter === 'review_required' ? '' : 'review_required');
          setOffset(0);
        },
      });
    }
    return chips;
  }, [businessStatusFilter, clearAllFilters, sourceFilter, summary, syncStatusFilter]);

  const page = state.data;

  return (
    <PanelCard
      eyebrow="Metadata"
      title="文件元数据"
      description="管理员视角的文件元数据；展示来源系统、同步状态与 RAGFlow 映射。"
      status={state.status}
      actions={(
        <button
          type="button"
          onClick={() => { reload(); void loadSummary(); }}
          className="console-icon-button"
          aria-label="刷新文件元数据"
        >
          <RefreshCw size={16} />
        </button>
      )}
      testId="console-meta-documents-card"
    >
      <MetadataToolbar
        onApply={applyFilters}
        onReset={resetFilters}
        activeFilters={activeFilters}
        totalCount={summary?.documents.total ?? null}
        totalLabel="文档"
        extra={(
          <ColumnMenu
            columns={DOC_COLUMNS}
            hiddenColumns={hiddenColumns}
            onToggle={toggleDocColumn}
            onReset={resetDocColumns}
          />
        )}
      >
        <label htmlFor="document-source-filter">来源系统</label>
        <input
          id="document-source-filter"
          value={sourceDraft}
          placeholder="如 EAM"
          onChange={(event) => setSourceDraft(event.target.value)}
        />
        <ToolbarSelect
          id="document-sync-filter"
          label="同步状态"
          value={syncStatusFilter}
          options={SYNC_STATUS_OPTIONS}
          onChange={(value) => { setSyncStatusFilter(value); setOffset(0); }}
        />
        <ToolbarSelect
          id="document-business-filter"
          label="业务状态"
          value={businessStatusFilter}
          options={BUSINESS_STATUS_OPTIONS}
          onChange={(value) => { setBusinessStatusFilter(value); setOffset(0); }}
        />
      </MetadataToolbar>
      {summary && (
        <MetadataSummaryStrip chips={summaryChips} testId="console-documents-summary" />
      )}
      {page?.items.length ? (
        <div className="console-table-wrap">
          <table className="console-table" data-testid="console-meta-documents-table">
            <thead>
              <tr>
                {visibleDocColumns.map((column) => (
                  column.sortField
                    ? <SortableTh key={column.key} label={column.label} field={column.sortField} sort={sort} onSort={handleSort} />
                    : <th key={column.key}>{column.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {page.items.map((item) => (
                <tr key={`${item.externalDocumentId}-${item.sourceVersionId}`}>
                  {visibleDocColumns.map((column) => renderDocumentCell(item, column.key))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="console-empty">
          {state.status === 'processing' ? '文件元数据加载中…' : '暂无文件元数据。'}
        </p>
      )}
      <MetadataPagination
        offset={offset}
        itemCount={page?.items.length ?? 0}
        hasMore={Boolean(page?.hasMore)}
        onPrev={() => setOffset(Math.max(0, offset - PAGE_LIMIT))}
        onNext={() => setOffset(offset + PAGE_LIMIT)}
      />
      {state.error && <PanelError error={state.error} onRetry={reload} />}
    </PanelCard>
  );
}
