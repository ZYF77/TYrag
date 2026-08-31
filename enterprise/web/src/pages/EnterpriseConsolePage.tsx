import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { ArrowUpRight, RefreshCw } from 'lucide-react';
import { API_MODE } from '../api/mode';
import { getHarnessToken, setHarnessToken, toDisplayError, v2Api } from '../api/v2Client';
import type {
  ConsoleModuleStatus,
  ConsoleState,
  ConsoleUserPrincipal,
  GatewayHealth,
} from '../api/consoleTypes';
import type {
  Citation,
  ConversationAttachmentResponse,
  ConversationDetail,
  ConversationSummary,
  DisplayError,
  Message,
} from '../api/v2Types';
import { TransientAttachmentPanel } from '../components/harness/TransientAttachmentPanel';
import { ConversationAdminPanel } from '../components/console/ConversationAdminPanel';
import { RagDiagnosticsPanel } from '../components/console/RagDiagnosticsPanel';
import { ConversationMetadataPanel, DocumentMetadataPanel, IntegrationsPanel } from '../components/console/SystemSettingsPanels';
import { WorkbenchShell, useWorkbenchTab } from '../components/layout/WorkbenchShell';
import './enterprise-console.css';

const CONSOLE_TABS = [
  'service',
  'sessions',
  'attachment',
  'integrations',
  'meta-conversations',
  'conversation-admin',
  'meta-documents',
  'rag-diagnostics',
] as const;
type ConsoleTab = (typeof CONSOLE_TABS)[number];

const CONSOLE_NAV = [
  {
    id: 'diagnostics',
    label: '诊断',
    items: [
      { id: 'service', label: '服务状态' },
      { id: 'sessions', label: '会话历史' },
      { id: 'attachment', label: '临时附件' },
    ],
  },
];

const SYSTEM_NAV_GROUP = {
  id: 'system',
  label: '系统设置',
  items: [
    { id: 'integrations', label: '接口配置' },
    { id: 'meta-conversations', label: '会话元数据' },
    { id: 'conversation-admin', label: '会话管理' },
    { id: 'meta-documents', label: '文件元数据' },
    { id: 'rag-diagnostics', label: 'RAG 诊断' },
  ],
};

const SYSTEM_TAB_IDS: readonly string[] = SYSTEM_NAV_GROUP.items.map((item) => item.id);

function initialState<T>(): ConsoleState<T> {
  return { status: 'processing', data: null, error: null };
}

function errorStatus(error: DisplayError): ConsoleModuleStatus {
  if (error.httpStatus === 401 || error.httpStatus === 403) return 'unauthorized';
  if (error.httpStatus === 0 || error.httpStatus === 502 || error.httpStatus === 503) {
    return 'unavailable';
  }
  return 'failed';
}

function statusText(status: ConsoleModuleStatus): string {
  return status;
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

function shorten(value: string | null | undefined, limit = 180): string {
  if (!value) return '未提供';
  return value.length > limit ? `${value.slice(0, limit)}…` : value;
}

async function encodeAttachment(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result !== 'string') {
        reject(new Error('Attachment content could not be read'));
        return;
      }
      const separator = reader.result.indexOf(',');
      resolve(separator >= 0 ? reader.result.slice(separator + 1) : reader.result);
    };
    reader.onerror = () => reject(reader.error ?? new Error('Attachment content could not be read'));
    reader.readAsDataURL(file);
  });
}

function StatusBadge({ status, testId }: { status: ConsoleModuleStatus; testId?: string }) {
  return (
    <span data-testid={testId} className={`console-status console-status--${status}`}>
      <span className="console-status-dot" aria-hidden="true" />
      {statusText(status)}
    </span>
  );
}

function ConsoleCard({
  eyebrow,
  title,
  description,
  status,
  children,
  actions,
  testId,
}: {
  eyebrow: string;
  title: string;
  description: string;
  status: ConsoleModuleStatus;
  children: ReactNode;
  actions?: ReactNode;
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
          <StatusBadge status={status} />
          {actions}
        </div>
      </div>
      <div className="console-card-body">{children}</div>
    </section>
  );
}

function ModuleError({ error, onRetry }: { error: DisplayError; onRetry: () => void }) {
  return (
    <div role="alert" className="console-alert">
      <p><strong>{error.code}</strong>{error.httpStatus ? ` · HTTP ${error.httpStatus}` : ''} · {error.message}</p>
      <button type="button" onClick={onRetry} className="console-secondary-button">重试</button>
    </div>
  );
}

function ProbeRow({
  label,
  route,
  state,
}: {
  label: string;
  route: string;
  state: ConsoleState<unknown>;
}) {
  return (
    <div className="console-row">
      <div>
        <p>{label}</p>
        <p className="console-route">{route}</p>
      </div>
      <StatusBadge status={state.status} />
    </div>
  );
}

function ServicePanel({
  health,
  identity,
  onRefresh,
}: {
  health: ConsoleState<GatewayHealth>;
  identity: ConsoleState<ConsoleUserPrincipal>;
  onRefresh: () => void;
}) {
  const status = health.status === 'healthy' && identity.status === 'healthy'
    ? 'healthy'
    : health.status === 'unavailable' || identity.status === 'unavailable'
      ? 'unavailable'
      : health.status === 'unauthorized' || identity.status === 'unauthorized'
        ? 'unauthorized'
        : 'processing';
  const modeLabel = API_MODE === 'mock' ? 'mock / MSW' : `${API_MODE} / public Gateway routes`;
  return (
    <ConsoleCard
      eyebrow="Gateway"
      title="服务与用户边界"
      description="只探测公开 Gateway；健康探针与用户 JWT 会话分开显示，任何一项失败都不会阻断其他卡片。"
      status={status}
      actions={<button type="button" onClick={onRefresh} className="console-icon-button" aria-label="刷新服务状态"><RefreshCw size={16} /></button>}
      testId="console-service-card"
    >
      <ProbeRow label="Gateway liveness" route="GET /enterprise/api/v1/health" state={health} />
      <ProbeRow label="User scope" route="GET /enterprise/api/v1/auth/me" state={identity} />
      <div className="console-note">
        <p><strong>运行边界</strong> · {modeLabel}</p>
        <p>浏览器只携带用户 Bearer。HMAC secret 不进入浏览器；User JWT 沿用现有 Harness 的 sessionStorage 生命周期，Console 不展示 JWT。</p>
        {health.data && <p className="console-route">gateway version · {health.data.version}</p>}
        {identity.data && <p>用户映射：{identity.data.mappingStatus} · capabilities {identity.data.capabilities.length}</p>}
      </div>
      {health.error && <ModuleError error={health.error} onRetry={onRefresh} />}
      {identity.error && <ModuleError error={identity.error} onRetry={onRefresh} />}
    </ConsoleCard>
  );
}

function MessageRow({ message, onCitation }: { message: Message; onCitation: (citation: Citation) => void }) {
  return (
    <article className="console-row console-row-start">
      <div>
        <p>{message.role === 'user' ? '用户消息 · 内容已隐藏' : '助手消息 · 原始回答不回显'}</p>
        <p className="console-route">{formatTime(message.createdAt)} · citations {message.citations.length}</p>
        {message.citations.length > 0 && (
          <div className="console-citation-links">
            {message.citations.map((citation, index) => (
              <button type="button" key={citation.citationId} onClick={() => onCitation(citation)} className="console-link-btn">
                citation {index + 1} · {shorten(citation.title, 42)}
              </button>
            ))}
          </div>
        )}
      </div>
      <span className="console-route">{message.status}</span>
    </article>
  );
}

function CitationPanel({
  state,
  onRetry,
}: {
  state: ConsoleState<Citation>;
  onRetry: () => void;
}) {
  if (state.status === 'configured') {
    return <p className="console-empty">选择一条 citation 重新走 Gateway 鉴权。</p>;
  }
  if (state.error) return <ModuleError error={state.error} onRetry={onRetry} />;
  if (!state.data) return <p className="console-empty">citation 加载中…</p>;
  return (
    <div>
      <div className="console-row">
        <p>{state.data.title}</p>
        <StatusBadge status="healthy" />
      </div>
      <dl className="console-metrics">
        <div><dt>sourceType</dt><dd>{state.data.sourceType}</dd></div>
        <div><dt>page</dt><dd>{state.data.pageNo ?? '未提供'}</dd></div>
        <div><dt>external document</dt><dd>{state.data.externalDocumentId ?? '未提供'}</dd></div>
        <div><dt>version</dt><dd>{state.data.sourceVersionId ?? '未提供'}</dd></div>
      </dl>
      <p className="console-hint">{shorten(state.data.excerpt)}</p>
      <p className="console-hint">仅展示已授权 citation snapshot，不展示存储路径或内部引擎标识。</p>
    </div>
  );
}

function ConversationPanel({
  state,
  history,
  citation,
  activeId,
  onRefresh,
  onCreate,
  onSelect,
  onCitation,
  onRetryHistory,
  onRetryCitation,
}: {
  state: ConsoleState<ConversationSummary[]>;
  history: ConsoleState<{ conversation: ConversationDetail; messages: Message[] }>;
  citation: ConsoleState<Citation>;
  activeId: string | null;
  onRefresh: () => void;
  onCreate: () => void;
  onSelect: (id: string) => void;
  onCitation: (citation: Citation) => void;
  onRetryHistory: () => void;
  onRetryCitation: () => void;
}) {
  const historyStatus = history.status === 'configured' ? 'configured' : history.status;
  return (
    <ConsoleCard
      eyebrow="Sessions"
      title="会话、历史与 citation"
      description="读取拥有者范围内的会话和持久历史；消息正文不在 Console 回显，citation 详情单独重新鉴权。"
      status={state.status}
      actions={<><button type="button" onClick={onCreate} className="console-secondary-button">新建诊断会话</button><button type="button" onClick={onRefresh} className="console-icon-button" aria-label="刷新会话列表"><RefreshCw size={16} /></button></>}
      testId="console-conversation-card"
    >
      <div className="console-split">
        <div>
          <div className="console-row">
            <p className="console-eyebrow">owned sessions</p>
            <span className="console-route">{state.data?.length ?? 0}</span>
          </div>
          {state.data?.map((item) => (
            <button type="button" key={item.conversationId} onClick={() => onSelect(item.conversationId)} className={`console-list-btn ${item.conversationId === activeId ? 'is-active' : ''}`}>
              <p>{item.title || '未命名会话'}</p>
              <p className="console-route">{item.status} · context v{item.contextVersion}</p>
            </button>
          ))}
          {!state.data?.length && <p className="console-empty">暂无会话，创建一个诊断会话。</p>}
          {state.error && <ModuleError error={state.error} onRetry={onRefresh} />}
        </div>
        <div>
          {history.data?.conversation && (
            <>
              <div className="console-row">
                <div>
                  <p>{history.data.conversation.title || '未命名会话'}</p>
                  <p className="console-route">{history.data.conversation.status} · context v{history.data.conversation.contextVersion}</p>
                </div>
                <StatusBadge status="healthy" />
              </div>
              <div className="console-hint console-chip-row">
                {history.data.conversation.equipmentId && <span className="console-chip">equipment · {history.data.conversation.equipmentId}</span>}
                {history.data.conversation.fixedAssetNo && <span className="console-chip">asset · {history.data.conversation.fixedAssetNo}</span>}
                {history.data.conversation.faultCode && <span className="console-chip">fault · {history.data.conversation.faultCode}</span>}
              </div>
            </>
          )}
          <div className="console-row">
            <p className="console-eyebrow">persisted history</p>
            <StatusBadge status={historyStatus} />
          </div>
          {history.data?.messages.length ? (
            history.data.messages.map((message) => <MessageRow key={message.messageId} message={message} onCitation={onCitation} />)
          ) : (
            <p className="console-empty">选择会话后显示历史状态；正文保持隐藏。</p>
          )}
          {history.error && <ModuleError error={history.error} onRetry={onRetryHistory} />}
          <div className="console-row">
            <p className="console-eyebrow">citation snapshot</p>
            <StatusBadge status={citation.status} />
          </div>
          <CitationPanel state={citation} onRetry={onRetryCitation} />
        </div>
      </div>
    </ConsoleCard>
  );
}

function AttachmentPanel({
  activeId,
  state,
  attachment,
  notice,
  onUpload,
  onIssueTicket,
  onDownload,
}: {
  activeId: string | null;
  state: ConsoleState<ConversationAttachmentResponse>;
  attachment: ConversationAttachmentResponse | null;
  notice: string | null;
  onUpload: (file: File) => void;
  onIssueTicket: () => void;
  onDownload: () => void;
}) {
  return (
    <div data-testid="console-attachment-card" className="console-card console-embedded">
      <div className="console-card-head">
        <div>
          <p className="console-eyebrow">Attachment</p>
          <h2>Transient attachment</h2>
          <p>正式 create → ticket → download 诊断入口。失败只影响本卡片，不改变 FILE_SHARE 或会话状态。</p>
        </div>
        <StatusBadge status={state.status === 'processing' ? 'processing' : state.status} />
      </div>
      <div className="console-card-body">
        {!activeId && <p className="console-hint">先在“会话、历史与 citation”中选择或创建会话。</p>}
        <TransientAttachmentPanel
          conversationId={activeId}
          loading={state.status === 'processing'}
          error={state.error}
          notice={notice}
          attachment={attachment}
          status={state.status}
          issueLoading={state.status === 'processing'}
          downloadLoading={state.status === 'processing'}
          onUpload={onUpload}
          onIssueTicket={onIssueTicket}
          onDownload={onDownload}
        />
      </div>
    </div>
  );
}

export function EnterpriseConsolePage() {
  const [tab, setTab] = useWorkbenchTab<ConsoleTab>('service', CONSOLE_TABS);
  const [health, setHealth] = useState<ConsoleState<GatewayHealth>>(initialState);
  const [identity, setIdentity] = useState<ConsoleState<ConsoleUserPrincipal>>(initialState);
  const [conversations, setConversations] = useState<ConsoleState<ConversationSummary[]>>(initialState);
  const [history, setHistory] = useState<ConsoleState<{ conversation: ConversationDetail; messages: Message[] }>>({ status: 'configured', data: null, error: null });
  const [citation, setCitation] = useState<ConsoleState<Citation>>({ status: 'configured', data: null, error: null });
  const [citationSelection, setCitationSelection] = useState<Citation | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [attachmentState, setAttachmentState] = useState<ConsoleState<ConversationAttachmentResponse>>({ status: 'configured', data: null, error: null });
  const [attachment, setAttachment] = useState<ConversationAttachmentResponse | null>(null);
  const [attachmentNotice, setAttachmentNotice] = useState<string | null>(null);
  const [tokenDraft, setTokenDraft] = useState('');
  const [tokenConfigured, setTokenConfigured] = useState(() => Boolean(getHarnessToken()));
  const isAdmin = identity.data?.capabilities.includes('admin') ?? false;
  const navGroups = useMemo(
    () => (isAdmin ? [...CONSOLE_NAV, SYSTEM_NAV_GROUP] : CONSOLE_NAV),
    [isAdmin],
  );

  const loadHealth = useCallback(async () => {
    setHealth({ status: 'processing', data: null, error: null });
    try {
      const data = await v2Api.getHealth();
      setHealth({ status: data.status === 'healthy' ? 'healthy' : 'failed', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setHealth({ status: errorStatus(displayError), data: null, error: displayError });
    }
  }, []);

  const loadIdentity = useCallback(async () => {
    setIdentity({ status: 'processing', data: null, error: null });
    try {
      const data = await v2Api.getAuthMe();
      setIdentity({ status: 'healthy', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setIdentity({ status: errorStatus(displayError), data: null, error: displayError });
    }
  }, []);

  const loadConversations = useCallback(async () => {
    setConversations({ status: 'processing', data: null, error: null });
    try {
      const page = await v2Api.listConversations();
      setConversations({ status: 'healthy', data: page.items, error: null });
      setActiveId((current) => current ?? page.items[0]?.conversationId ?? null);
    } catch (error) {
      const displayError = toDisplayError(error);
      setConversations({ status: errorStatus(displayError), data: null, error: displayError });
    }
  }, []);

  const loadHistory = useCallback(async (conversationId: string) => {
    setHistory({ status: 'processing', data: null, error: null });
    try {
      const [conversationResult, messageResult] = await Promise.allSettled([
        v2Api.getConversation(conversationId),
        v2Api.listMessages(conversationId),
      ]);
      const conversationError = conversationResult.status === 'rejected' ? toDisplayError(conversationResult.reason) : null;
      const messageError = messageResult.status === 'rejected' ? toDisplayError(messageResult.reason) : null;
      const error = conversationError ?? messageError;
      if (conversationResult.status === 'fulfilled' && messageResult.status === 'fulfilled') {
        setHistory({ status: 'healthy', data: { conversation: conversationResult.value, messages: messageResult.value.items }, error: null });
      } else {
        setHistory({ status: error ? errorStatus(error) : 'failed', data: null, error });
      }
    } catch (error) {
      const displayError = toDisplayError(error);
      setHistory({ status: errorStatus(displayError), data: null, error: displayError });
    }
  }, []);

  useEffect(() => {
    void loadHealth();
    void loadIdentity();
    void loadConversations();
  }, [loadConversations, loadHealth, loadIdentity]);

  useEffect(() => {
    setCitation({ status: 'configured', data: null, error: null });
    setCitationSelection(null);
    setAttachment(null);
    setAttachmentState({ status: 'configured', data: null, error: null });
    setAttachmentNotice(null);
    if (activeId) void loadHistory(activeId);
    else setHistory({ status: 'configured', data: null, error: null });
  }, [activeId, loadHistory]);

  const refreshAll = useCallback(() => {
    void loadHealth();
    void loadIdentity();
    void loadConversations();
  }, [loadConversations, loadHealth, loadIdentity]);

  const saveToken = useCallback(() => {
    setHarnessToken(tokenDraft);
    setTokenDraft('');
    setTokenConfigured(Boolean(getHarnessToken()));
    refreshAll();
  }, [refreshAll, tokenDraft]);

  const createConversation = useCallback(async () => {
    setConversations((current) => ({ ...current, status: 'processing', error: null }));
    try {
      const detail = await v2Api.createConversation({});
      setConversations((current) => ({ status: 'healthy', data: [detail, ...(current.data ?? []).filter((item) => item.conversationId !== detail.conversationId)], error: null }));
      setActiveId(detail.conversationId);
    } catch (error) {
      const displayError = toDisplayError(error);
      setConversations((current) => ({ ...current, status: errorStatus(displayError), error: displayError }));
    }
  }, []);

  const selectCitation = useCallback(async (snapshot: Citation) => {
    setCitationSelection(snapshot);
    setCitation({ status: 'processing', data: null, error: null });
    try {
      const data = await v2Api.getCitation(snapshot.citationId);
      setCitation({ status: 'healthy', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setCitation({ status: errorStatus(displayError), data: null, error: displayError });
    }
  }, []);

  const uploadAttachment = useCallback(async (file: File) => {
    if (!activeId) return;
    setAttachmentState({ status: 'processing', data: null, error: null });
    setAttachmentNotice(null);
    try {
      const created = await v2Api.createConversationAttachment(activeId, {
        fileName: file.name,
        mediaType: file.type || 'application/octet-stream',
        content: await encodeAttachment(file),
      });
      const ticketed = await v2Api.issueConversationAttachmentTicket(created.attachmentId);
      setAttachment(ticketed);
      setAttachmentState({ status: 'retrievable', data: ticketed, error: null });
      setAttachmentNotice('create 与 ticket 已完成；下载响应体不会在 Console 展示。');
    } catch (error) {
      const displayError = toDisplayError(error);
      setAttachmentState({ status: errorStatus(displayError), data: null, error: displayError });
    }
  }, [activeId]);

  const issueTicket = useCallback(async () => {
    if (!attachment) return;
    setAttachmentState({ status: 'processing', data: attachment, error: null });
    setAttachmentNotice(null);
    try {
      const ticketed = await v2Api.issueConversationAttachmentTicket(attachment.attachmentId);
      setAttachment(ticketed);
      setAttachmentState({ status: 'retrievable', data: ticketed, error: null });
      setAttachmentNotice('新下载票据已签发；票据本身不会显示。');
    } catch (error) {
      const displayError = toDisplayError(error);
      setAttachmentState({ status: errorStatus(displayError), data: attachment, error: displayError });
    }
  }, [attachment]);

  const verifyDownload = useCallback(async () => {
    if (!attachment) return;
    setAttachmentState({ status: 'processing', data: attachment, error: null });
    setAttachmentNotice(null);
    try {
      const result = await v2Api.verifyConversationAttachmentDownload(attachment);
      setAttachmentState({ status: 'retrievable', data: attachment, error: null });
      setAttachmentNotice(`download route verified · ${result.sizeBytes} bytes · ${result.contentType}`);
    } catch (error) {
      const displayError = toDisplayError(error);
      setAttachmentState({ status: errorStatus(displayError), data: attachment, error: displayError });
    }
  }, [attachment]);

  const activeSummary = useMemo(() => conversations.data?.find((item) => item.conversationId === activeId) ?? null, [activeId, conversations.data]);

  return (
    <WorkbenchShell
      testId="console-page"
      shellClass="console-shell"
      brand="Console"
      subtitle="Gateway diagnostics"
      groups={navGroups}
      activeId={tab}
      onSelect={(id) => setTab(id as ConsoleTab)}
      actions={(
        <>
          <span className="console-mode-badge">{API_MODE === 'mock' ? 'TEST · MSW' : `${API_MODE.toUpperCase()} · PUBLIC API`}</span>
          <span className="console-mode-badge">{tokenConfigured ? 'Bearer 已注入' : '无 Bearer（可测试 401）'}</span>
          <a href="/" className="console-secondary-button">返回 Harness <ArrowUpRight size={14} /></a>
        </>
      )}
      tokenRow={(
        <div className="console-token-row">
          <label htmlFor="console-token">运行期 Bearer（不写入源码）</label>
          <input id="console-token" type="password" value={tokenDraft} onChange={(event) => setTokenDraft(event.target.value)} placeholder={tokenConfigured ? '已配置，可留空' : '仅本地联调注入'} />
          <button type="button" onClick={saveToken} className="console-primary-button">保存运行期凭据</button>
          <span className="console-route">不显示、不记录 Token。</span>
        </div>
      )}
      footer={(
        <footer className="console-footer">
          <span>Active trace: {activeSummary ? 'conversation selected' : 'no conversation selected'}</span>
          <span className="console-route">route /console · Attachment contract v2.1.0</span>
        </footer>
      )}
    >
      {tab === 'service' && (
        <ServicePanel health={health} identity={identity} onRefresh={refreshAll} />
      )}
      {tab === 'sessions' && (
        <ConversationPanel
          state={conversations}
          history={history}
          citation={citation}
          activeId={activeId}
          onRefresh={loadConversations}
          onCreate={() => void createConversation()}
          onSelect={setActiveId}
          onCitation={(item) => void selectCitation(item)}
          onRetryHistory={() => { if (activeId) void loadHistory(activeId); }}
          onRetryCitation={() => { if (citationSelection) void selectCitation(citationSelection); }}
        />
      )}
      {tab === 'attachment' && (
        <AttachmentPanel
          activeId={activeId}
          state={attachmentState}
          attachment={attachment}
          notice={attachmentNotice}
          onUpload={(file) => void uploadAttachment(file)}
          onIssueTicket={() => void issueTicket()}
          onDownload={() => void verifyDownload()}
        />
      )}
      {isAdmin && tab === 'integrations' && <IntegrationsPanel />}
      {isAdmin && tab === 'meta-conversations' && <ConversationMetadataPanel />}
      {isAdmin && tab === 'conversation-admin' && <ConversationAdminPanel />}
      {isAdmin && tab === 'meta-documents' && <DocumentMetadataPanel />}
      {isAdmin && tab === 'rag-diagnostics' && <RagDiagnosticsPanel />}
      {!isAdmin && SYSTEM_TAB_IDS.includes(tab) && (
        <p className="console-empty">需要 admin capability 才能查看系统设置。</p>
      )}
    </WorkbenchShell>
  );
}
