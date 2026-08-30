import { useCallback, useEffect, useMemo, useState } from 'react';
import { ArrowLeft, RefreshCw } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import { toDisplayError, v2Api } from '../../api/v2Client';
import type {
  AdminConversationMessage,
  ConsoleModuleStatus,
  ConsoleState,
  ConversationMetadataItem,
  ConversationMetadataOrderBy,
  ConversationMetadataPage,
  MetadataSummary,
} from '../../api/consoleTypes';
import type { DisplayError } from '../../api/v2Types';
import {
  ColumnMenu,
  DEFAULT_SORT_STATE,
  MetadataPagination,
  MetadataSummaryStrip,
  MetadataToolbar,
  PanelCard,
  PanelError,
  SortableTh,
  StatusPill,
  formatTime,
  nextSortState,
  panelErrorStatus,
  useHiddenTableColumns,
  type MetadataActiveFilter,
  type MetadataSortState,
  type MetadataSummaryChip,
} from './SystemSettingsPanels';

const PAGE_LIMIT = 50;
const NOT_PROVIDED = '未提供';

type AdminConversationColumnKey =
  | 'conversationId'
  | 'businessUserId'
  | 'equipmentId'
  | 'fixedAssetNo'
  | 'status'
  | 'lastMessageAt'
  | 'actions';

const ADMIN_CONVERSATION_COLUMNS: Array<{ key: AdminConversationColumnKey; label: string; sortField: ConversationMetadataOrderBy | null; fixed?: boolean }> = [
  { key: 'conversationId', label: '会话', sortField: 'conversationId', fixed: true },
  { key: 'businessUserId', label: '业务用户', sortField: 'businessUserId' },
  { key: 'equipmentId', label: '设备', sortField: 'equipmentId' },
  { key: 'fixedAssetNo', label: '固定资产', sortField: 'fixedAssetNo' },
  { key: 'status', label: '状态', sortField: 'status' },
  { key: 'lastMessageAt', label: '最近消息', sortField: 'lastMessageAt' },
  { key: 'actions', label: '操作', sortField: null, fixed: true },
];

const ADMIN_CONVERSATION_HIDDEN_COLUMNS_KEY = 'console.convAdmin.hiddenColumns';
const ADMIN_CONVERSATION_DEFAULT_HIDDEN_COLUMNS: string[] = [];

// 与后端 public_status（gateway/query/v2_store.py）一致的中文映射；未知码原样展示。
const MESSAGE_STATUS_LABELS: Record<string, string> = {
  completed: '已完成',
  no_reliable_evidence: '无可靠依据',
  failed: '失败',
  running: '处理中',
  active: '进行中',
  archived: '已归档',
};

function messageStatusLabel(status: string): string {
  return MESSAGE_STATUS_LABELS[status] ?? status;
}

function renderAdminConversationCell(
  item: ConversationMetadataItem,
  key: AdminConversationColumnKey,
  onOpen: (item: ConversationMetadataItem) => void,
): React.ReactNode {
  switch (key) {
    case 'conversationId':
      return <td key={key} className="console-table-mono">{item.conversationId}</td>;
    case 'businessUserId':
      return <td key={key}>{item.businessUserId}</td>;
    case 'equipmentId':
      return <td key={key}>{item.equipmentId ?? NOT_PROVIDED}</td>;
    case 'fixedAssetNo':
      return <td key={key}>{item.fixedAssetNo ?? NOT_PROVIDED}</td>;
    case 'status':
      return <td key={key}><StatusPill code={item.status} /></td>;
    case 'lastMessageAt':
      return <td key={key}>{formatTime(item.lastMessageAt)}</td>;
    case 'actions':
      return (
        <td key={key} className="console-col-center">
          <button type="button" className="console-secondary-button" onClick={() => onOpen(item)}>
            查看对话
          </button>
        </td>
      );
    default:
      return null;
  }
}

function initialState(): ConsoleState<ConversationMetadataPage> {
  return { status: 'processing', data: null, error: null };
}

interface ConversationDetailState {
  conversation: ConversationMetadataItem;
  status: 'processing' | 'healthy' | 'failed';
  messages: AdminConversationMessage[];
  error: DisplayError | null;
}

function AdminMessageBubble({ message }: { message: AdminConversationMessage }) {
  const isUser = message.role === 'user';
  return (
    <article className={`console-chat-bubble console-chat-bubble--${isUser ? 'user' : 'assistant'}`}>
      <div className="console-chat-meta">
        <span>{isUser ? '用户' : 'EAM 回复'}</span>
        <span>{formatTime(message.createdAt)}</span>
        <StatusPill code={message.status} label={messageStatusLabel(message.status)} />
      </div>
      <div className="console-chat-content">
        {isUser ? (
          <p className="console-chat-text">{message.content}</p>
        ) : (
          <ReactMarkdown>{message.content}</ReactMarkdown>
        )}
      </div>
    </article>
  );
}

export function ConversationAdminPanel() {
  const [state, setState] = useState<ConsoleState<ConversationMetadataPage>>(initialState);
  const [statusFilter, setStatusFilter] = useState('');
  const [sort, setSort] = useState<MetadataSortState>(DEFAULT_SORT_STATE);
  const [offset, setOffset] = useState(0);
  const [requestToken, setRequestToken] = useState(0);
  const [summary, setSummary] = useState<MetadataSummary | null>(null);
  const [detail, setDetail] = useState<ConversationDetailState | null>(null);
  const { hiddenColumns: hiddenAdminColumns, visibleColumns: visibleAdminColumns, toggleColumn: toggleAdminColumn, resetColumns: resetAdminColumns } = useHiddenTableColumns(
    ADMIN_CONVERSATION_HIDDEN_COLUMNS_KEY,
    ADMIN_CONVERSATION_DEFAULT_HIDDEN_COLUMNS,
    ADMIN_CONVERSATION_COLUMNS,
  );

  const reload = useCallback(() => setRequestToken((token) => token + 1), []);

  const load = useCallback(async () => {
    setState(initialState());
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

  const openConversation = useCallback(async (conversation: ConversationMetadataItem) => {
    setDetail({ conversation, status: 'processing', messages: [], error: null });
    try {
      const page = await v2Api.getAdminConversationMessages(conversation.conversationId);
      setDetail({ conversation, status: 'healthy', messages: page.items, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setDetail({ conversation, status: 'failed', messages: [], error: displayError });
    }
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

  const cardStatus: ConsoleModuleStatus = detail
    ? detail.status === 'healthy'
      ? 'healthy'
      : detail.status === 'processing'
        ? 'processing'
        : detail.error
          ? panelErrorStatus(detail.error)
          : 'failed'
    : state.status;

  const page = state.data;

  return (
    <PanelCard
      eyebrow="Sessions"
      title="会话管理"
      description="列出全部会话；点击“查看对话”展示完整对话。消息状态为持久化的业务状态，原样展示，不按 citations 推导。"
      status={cardStatus}
      actions={(
        <button
          type="button"
          onClick={() => { reload(); void loadSummary(); }}
          className="console-icon-button"
          aria-label="刷新会话列表"
        >
          <RefreshCw size={16} />
        </button>
      )}
      testId="console-admin-conversations-card"
    >
      {detail ? (
        <>
          <div className="console-row">
            <button type="button" className="console-secondary-button" onClick={() => setDetail(null)}>
              <ArrowLeft size={14} /> 返回列表
            </button>
            <div className="console-chip-row">
              <span className="console-chip">业务用户 · {detail.conversation.businessUserId}</span>
              {detail.conversation.equipmentId && <span className="console-chip">设备 · {detail.conversation.equipmentId}</span>}
              {detail.conversation.fixedAssetNo && <span className="console-chip">固定资产 · {detail.conversation.fixedAssetNo}</span>}
              <StatusPill code={detail.conversation.status} />
              <span className="console-chip">context v{detail.conversation.contextVersion}</span>
            </div>
          </div>
          {detail.status === 'processing' && <p className="console-hint">对话消息加载中…</p>}
          {detail.error && (
            <PanelError
              error={detail.error}
              onRetry={() => void openConversation(detail.conversation)}
            />
          )}
          {detail.status === 'healthy' && (
            <div className="console-chat" data-testid="console-admin-chat">
              {detail.messages.length ? (
                detail.messages.map((message) => (
                  <AdminMessageBubble key={message.messageId} message={message} />
                ))
              ) : (
                <p className="console-empty">该会话暂无持久化消息。</p>
              )}
            </div>
          )}
        </>
      ) : (
        <>
          <MetadataToolbar
            onApply={applyFilters}
            onReset={resetFilters}
            activeFilters={activeFilters}
            totalCount={summary?.conversations.total ?? null}
            totalLabel="会话"
            extra={(
              <ColumnMenu
                columns={ADMIN_CONVERSATION_COLUMNS}
                hiddenColumns={hiddenAdminColumns}
                onToggle={toggleAdminColumn}
                onReset={resetAdminColumns}
              />
            )}
          >
            <label htmlFor="admin-conversation-status-filter">状态</label>
            <select
              id="admin-conversation-status-filter"
              value={statusFilter}
              onChange={(event) => { setStatusFilter(event.target.value); setOffset(0); }}
            >
              <option value="">全部</option>
              <option value="active">active</option>
              <option value="archived">archived</option>
            </select>
          </MetadataToolbar>
          {summary && (
            <MetadataSummaryStrip
              chips={summaryChips}
              testId="console-admin-conversations-summary"
            />
          )}
          {page?.items.length ? (
            <div className="console-table-wrap">
              <table className="console-table" data-testid="console-admin-conversations-table">
                <thead>
                  <tr>
                    {visibleAdminColumns.map((column) => (
                      column.sortField
                        ? <SortableTh key={column.key} label={column.label} field={column.sortField} sort={sort} onSort={handleSort} />
                        : <th key={column.key} className={column.key === 'actions' ? 'console-col-center' : undefined}>{column.label}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {page.items.map((item) => (
                    <tr key={item.conversationId}>
                      {visibleAdminColumns.map((column) => renderAdminConversationCell(item, column.key, openConversation))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="console-empty">
              {state.status === 'processing' ? '会话列表加载中…' : '暂无会话。'}
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
        </>
      )}
    </PanelCard>
  );
}
