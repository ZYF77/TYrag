import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { Activity, ArrowUpRight, FileClock, MessageSquareText, Paperclip, RefreshCw, ShieldCheck } from 'lucide-react';
import { API_MODE } from '../api/mode';
import { toDisplayError, v2Api } from '../api/v2Client';
import type {
  ConsoleModuleStatus,
  ConsoleState,
  ConsoleUserPrincipal,
  FileShareDocumentStatusPage,
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
import './enterprise-console.css';

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

function statusForDocumentPage(page: FileShareDocumentStatusPage): ConsoleModuleStatus {
  if (page.items.some((item) => item.retrievable)) return 'retrievable';
  if (page.items.some((item) => item.status === 'failed' || item.errorCode)) return 'failed';
  return 'processing';
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
  const tone = {
    configured: 'border-slate-200 bg-slate-50 text-slate-600',
    healthy: 'border-emerald-200 bg-emerald-50 text-emerald-700',
    unavailable: 'border-orange-200 bg-orange-50 text-orange-700',
    unauthorized: 'border-amber-200 bg-amber-50 text-amber-800',
    processing: 'border-blue-200 bg-blue-50 text-blue-700',
    retrievable: 'border-teal-200 bg-teal-50 text-teal-800',
    failed: 'border-rose-200 bg-rose-50 text-rose-700',
  }[status];
  return (
    <span data-testid={testId} className={`inline-flex items-center gap-1 rounded-full border px-2 py-1 text-[10px] font-bold uppercase tracking-[0.14em] ${tone}`}>
      <span className="console-status-dot" aria-hidden="true" />
      {statusText(status)}
    </span>
  );
}

function ConsoleCard({
  icon,
  eyebrow,
  title,
  description,
  status,
  children,
  actions,
  testId,
}: {
  icon: ReactNode;
  eyebrow: string;
  title: string;
  description: string;
  status: ConsoleModuleStatus;
  children: ReactNode;
  actions?: ReactNode;
  testId: string;
}) {
  return (
    <section data-testid={testId} className="console-card rounded-2xl p-4 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 gap-3">
          <div className="console-icon mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl" aria-hidden="true">
            {icon}
          </div>
          <div className="min-w-0">
            <p className="console-eyebrow">{eyebrow}</p>
            <h2 className="mt-1 text-[17px] font-semibold tracking-[-0.02em] text-slate-950">{title}</h2>
            <p className="mt-1 max-w-2xl text-xs leading-5 text-slate-500">{description}</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <StatusBadge status={status} />
          {actions}
        </div>
      </div>
      <div className="mt-4">{children}</div>
    </section>
  );
}

function ModuleError({ error, onRetry }: { error: DisplayError; onRetry: () => void }) {
  return (
    <div role="alert" className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2.5 text-xs text-rose-800">
      <p><strong>{error.code}</strong>{error.httpStatus ? ` · HTTP ${error.httpStatus}` : ''} · {error.message}</p>
      <button type="button" onClick={onRetry} className="rounded-md border border-rose-200 bg-white px-2.5 py-1.5 font-semibold text-rose-700 hover:bg-rose-100">重试</button>
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
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 py-3 last:border-b-0">
      <div>
        <p className="text-sm font-semibold text-slate-800">{label}</p>
        <p className="mt-0.5 font-mono text-[10px] text-slate-400">{route}</p>
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
      icon={<Activity size={18} />}
      eyebrow="01 · SERVICE SURFACE"
      title="服务与用户边界"
      description="只探测公开 Gateway；健康探针与用户 JWT 会话分开显示，任何一项失败都不会阻断其他卡片。"
      status={status}
      actions={<button type="button" onClick={onRefresh} className="console-icon-button" aria-label="刷新服务状态"><RefreshCw size={14} /></button>}
      testId="console-service-card"
    >
      <div className="grid gap-3 lg:grid-cols-[1.15fr_.85fr]">
        <div className="rounded-xl border border-slate-100 bg-white/70 px-3">
          <ProbeRow label="Gateway liveness" route="GET /enterprise/api/v1/health" state={health} />
          <ProbeRow label="User scope" route="GET /enterprise/api/v1/auth/me" state={identity} />
        </div>
        <div className="console-note rounded-xl p-3">
          <div className="flex items-center gap-2 text-xs font-semibold text-slate-800"><ShieldCheck size={15} />运行边界</div>
          <p className="mt-2 text-xs leading-5 text-slate-600">{modeLabel}</p>
          <p className="mt-2 text-[11px] leading-5 text-slate-500">浏览器只携带用户 Bearer。HMAC secret 不进入浏览器；User JWT 沿用现有 Harness 的 sessionStorage 生命周期，Console 不展示 JWT。</p>
          {health.data && <p className="mt-2 font-mono text-[10px] text-slate-400">gateway version · {health.data.version}</p>}
          {identity.data && <p className="mt-2 text-[11px] text-teal-700">用户映射：{identity.data.mappingStatus} · capabilities {identity.data.capabilities.length}</p>}
        </div>
      </div>
      {health.error && <ModuleError error={health.error} onRetry={onRefresh} />}
      {identity.error && <ModuleError error={identity.error} onRetry={onRefresh} />}
    </ConsoleCard>
  );
}

function DocumentPanel({
  state,
  onRefresh,
}: {
  state: ConsoleState<FileShareDocumentStatusPage>;
  onRefresh: () => void;
}) {
  return (
    <ConsoleCard
      icon={<FileClock size={18} />}
      eyebrow="02 · FILE_SHARE"
      title="文档处理与可检索性"
      description="读取 v3 FILE_SHARE 状态事实，不显示存储路径或引擎标识；retrievable 只来自 Gateway 返回值。"
      status={state.status}
      actions={<button type="button" onClick={onRefresh} className="console-icon-button" aria-label="刷新 FILE_SHARE 状态"><RefreshCw size={14} /></button>}
      testId="console-document-card"
    >
      <div className="mb-3 rounded-xl border border-amber-100 bg-amber-50/70 px-3 py-2 text-[11px] leading-5 text-amber-900">
        浏览器不生成 HMAC。真实 Gateway 下若未由服务侧签名，模块会如实显示 <strong>unauthorized</strong>。
      </div>
      {state.data?.items.length ? (
        <div className="space-y-2">
          {state.data.items.map((item) => (
            <article key={`${item.externalDocumentId}-${item.sourceVersionId}`} className="rounded-xl border border-slate-100 bg-white/70 p-3">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate text-sm font-semibold text-slate-800">{item.externalDocumentId}</p>
                  <p className="mt-1 text-[11px] text-slate-500">版本 {item.sourceVersionId} · {item.stage ?? 'stage 未提供'} · {item.status}</p>
                </div>
                <StatusBadge status={item.retrievable ? 'retrievable' : item.status === 'failed' ? 'failed' : 'processing'} />
              </div>
              <div className="mt-3 grid grid-cols-2 gap-2 text-[11px] sm:grid-cols-4">
                <div><p className="text-slate-400">parse</p><p className="mt-0.5 font-semibold text-slate-700">{item.parseCompleted ? 'complete' : 'processing'}</p></div>
                <div><p className="text-slate-400">index</p><p className="mt-0.5 font-semibold text-slate-700">{item.indexCompleted ? 'complete' : 'processing'}</p></div>
                <div><p className="text-slate-400">quality</p><p className="mt-0.5 font-semibold text-slate-700">{item.qualityStatus ?? 'unknown'}</p></div>
                <div><p className="text-slate-400">updated</p><p className="mt-0.5 font-semibold text-slate-700">{formatTime(item.updatedAt)}</p></div>
              </div>
              {item.error && <p className="mt-2 text-[11px] text-rose-700">{item.error.code} · {item.error.message}</p>}
            </article>
          ))}
        </div>
      ) : (
        <p className="rounded-xl border border-dashed border-slate-200 px-3 py-8 text-center text-xs text-slate-400">暂无 FILE_SHARE 状态。</p>
      )}
      {state.error && <ModuleError error={state.error} onRetry={onRefresh} />}
    </ConsoleCard>
  );
}

function MessageRow({ message, onCitation }: { message: Message; onCitation: (citation: Citation) => void }) {
  return (
    <article className="rounded-xl border border-slate-100 bg-white/70 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-semibold text-slate-800">{message.role === 'user' ? '用户消息 · 内容已隐藏' : '助手消息 · 原始回答不回显'}</span>
        <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-slate-400">{message.status}</span>
      </div>
      <p className="mt-1 text-[11px] text-slate-500">{formatTime(message.createdAt)} · citations {message.citations.length}</p>
      {message.citations.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {message.citations.map((citation, index) => (
            <button type="button" key={citation.citationId} onClick={() => onCitation(citation)} className="rounded-md border border-teal-200 bg-teal-50 px-2 py-1 text-[11px] font-medium text-teal-800 hover:bg-teal-100">
              citation {index + 1} · {shorten(citation.title, 42)}
            </button>
          ))}
        </div>
      )}
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
    return <div className="rounded-xl border border-dashed border-slate-200 px-3 py-5 text-xs text-slate-400">选择一条 citation 重新走 Gateway 鉴权。</div>;
  }
  if (state.error) return <ModuleError error={state.error} onRetry={onRetry} />;
  if (!state.data) return <p className="text-xs text-slate-400">citation 加载中…</p>;
  return (
    <div className="rounded-xl border border-teal-100 bg-teal-50/60 p-3 text-xs text-teal-950">
      <div className="flex items-center justify-between gap-2">
        <p className="font-semibold">{state.data.title}</p>
        <StatusBadge status="healthy" />
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-[11px]">
        <div><dt className="text-teal-700/70">sourceType</dt><dd className="mt-0.5 font-medium">{state.data.sourceType}</dd></div>
        <div><dt className="text-teal-700/70">page</dt><dd className="mt-0.5 font-medium">{state.data.pageNo ?? '未提供'}</dd></div>
        <div><dt className="text-teal-700/70">external document</dt><dd className="mt-0.5 break-all font-medium">{state.data.externalDocumentId ?? '未提供'}</dd></div>
        <div><dt className="text-teal-700/70">version</dt><dd className="mt-0.5 break-all font-medium">{state.data.sourceVersionId ?? '未提供'}</dd></div>
      </dl>
      <p className="mt-3 border-t border-teal-100 pt-2 leading-5 text-teal-900">{shorten(state.data.excerpt)}</p>
      <p className="mt-2 text-[10px] text-teal-800/70">仅展示已授权 citation snapshot，不展示存储路径或内部引擎标识。</p>
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
      icon={<MessageSquareText size={18} />}
      eyebrow="03 · SESSION TRACE"
      title="会话、历史与 citation"
      description="读取拥有者范围内的会话和持久历史；消息正文不在 Console 回显，citation 详情单独重新鉴权。"
      status={state.status}
      actions={<><button type="button" onClick={onCreate} className="console-secondary-button">新建诊断会话</button><button type="button" onClick={onRefresh} className="console-icon-button" aria-label="刷新会话列表"><RefreshCw size={14} /></button></>}
      testId="console-conversation-card"
    >
      <div className="grid gap-3 lg:grid-cols-[minmax(210px,.7fr)_minmax(0,1.3fr)]">
        <div className="rounded-xl border border-slate-100 bg-white/70 p-2">
          <div className="flex items-center justify-between px-2 py-1">
            <p className="console-eyebrow">owned sessions</p>
            <span className="text-[10px] text-slate-400">{state.data?.length ?? 0}</span>
          </div>
          <div className="mt-1 space-y-1">
            {state.data?.map((item) => (
              <button type="button" key={item.conversationId} onClick={() => onSelect(item.conversationId)} className={`w-full rounded-lg border px-2.5 py-2 text-left ${item.conversationId === activeId ? 'border-teal-300 bg-teal-50' : 'border-transparent hover:border-slate-200 hover:bg-slate-50'}`}>
                <p className="truncate text-xs font-semibold text-slate-800">{item.title || '未命名会话'}</p>
                <p className="mt-1 text-[10px] text-slate-500">{item.status} · context v{item.contextVersion}</p>
              </button>
            ))}
            {!state.data?.length && <p className="px-2 py-6 text-center text-xs text-slate-400">暂无会话，创建一个诊断会话。</p>}
          </div>
          {state.error && <ModuleError error={state.error} onRetry={onRefresh} />}
        </div>
        <div className="min-w-0 space-y-3">
          {history.data?.conversation && (
            <div className="rounded-xl border border-slate-100 bg-white/70 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div><p className="text-sm font-semibold text-slate-800">{history.data.conversation.title || '未命名会话'}</p><p className="mt-1 text-[11px] text-slate-500">{history.data.conversation.status} · context v{history.data.conversation.contextVersion}</p></div>
                <StatusBadge status="healthy" />
              </div>
              <div className="mt-3 flex flex-wrap gap-2 text-[10px] text-slate-500">
                {history.data.conversation.equipmentId && <span className="rounded-md bg-slate-100 px-2 py-1">equipment · {history.data.conversation.equipmentId}</span>}
                {history.data.conversation.fixedAssetNo && <span className="rounded-md bg-slate-100 px-2 py-1">asset · {history.data.conversation.fixedAssetNo}</span>}
                {history.data.conversation.faultCode && <span className="rounded-md bg-slate-100 px-2 py-1">fault · {history.data.conversation.faultCode}</span>}
              </div>
            </div>
          )}
          <div className="flex items-center justify-between gap-2">
            <p className="console-eyebrow">persisted history</p>
            <StatusBadge status={historyStatus} />
          </div>
          {history.data?.messages.length ? (
            <div className="space-y-2">
              {history.data.messages.map((message) => <MessageRow key={message.messageId} message={message} onCitation={onCitation} />)}
            </div>
          ) : (
            <p className="rounded-xl border border-dashed border-slate-200 px-3 py-6 text-center text-xs text-slate-400">选择会话后显示历史状态；正文保持隐藏。</p>
          )}
          {history.error && <ModuleError error={history.error} onRetry={onRetryHistory} />}
          <div className="border-t border-slate-100 pt-3">
            <div className="mb-2 flex items-center gap-2"><p className="console-eyebrow">citation snapshot</p><StatusBadge status={citation.status} /></div>
            <CitationPanel state={citation} onRetry={onRetryCitation} />
          </div>
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
    <div data-testid="console-attachment-card" className="console-card rounded-2xl p-4 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="flex gap-3"><div className="console-icon mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl"><Paperclip size={18} /></div><div><p className="console-eyebrow">04 · EPHEMERAL INPUT</p><h2 className="mt-1 text-[17px] font-semibold tracking-[-0.02em] text-slate-950">Transient attachment</h2><p className="mt-1 text-xs leading-5 text-slate-500">正式 create → ticket → download 诊断入口。失败只影响本卡片，不改变 FILE_SHARE 或会话状态。</p></div></div>
        <StatusBadge status={state.status === 'processing' ? 'processing' : state.status} />
      </div>
      <div className="mt-4">
        {!activeId && <div className="mb-3 rounded-xl border border-amber-100 bg-amber-50/70 px-3 py-2 text-[11px] text-amber-900">先在“会话、历史与 citation”中选择或创建会话。</div>}
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
  const [health, setHealth] = useState<ConsoleState<GatewayHealth>>(initialState);
  const [identity, setIdentity] = useState<ConsoleState<ConsoleUserPrincipal>>(initialState);
  const [documents, setDocuments] = useState<ConsoleState<FileShareDocumentStatusPage>>(initialState);
  const [conversations, setConversations] = useState<ConsoleState<ConversationSummary[]>>(initialState);
  const [history, setHistory] = useState<ConsoleState<{ conversation: ConversationDetail; messages: Message[] }>>({ status: 'configured', data: null, error: null });
  const [citation, setCitation] = useState<ConsoleState<Citation>>({ status: 'configured', data: null, error: null });
  const [citationSelection, setCitationSelection] = useState<Citation | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [attachmentState, setAttachmentState] = useState<ConsoleState<ConversationAttachmentResponse>>({ status: 'configured', data: null, error: null });
  const [attachment, setAttachment] = useState<ConversationAttachmentResponse | null>(null);
  const [attachmentNotice, setAttachmentNotice] = useState<string | null>(null);

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

  const loadDocuments = useCallback(async () => {
    setDocuments({ status: 'processing', data: null, error: null });
    try {
      const data = await v2Api.listFileShareStatuses();
      setDocuments({ status: statusForDocumentPage(data), data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setDocuments({ status: errorStatus(displayError), data: null, error: displayError });
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
    void loadDocuments();
    void loadConversations();
  }, [loadConversations, loadDocuments, loadHealth, loadIdentity]);

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
    void loadDocuments();
    void loadConversations();
  }, [loadConversations, loadDocuments, loadHealth, loadIdentity]);

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
    <main data-testid="console-page" className="console-shell min-h-screen overflow-x-hidden text-slate-900">
      <div className="console-halo" aria-hidden="true" />
      <div className="mx-auto max-w-[1440px] px-4 py-5 sm:px-6 lg:px-8 lg:py-8">
        <header className="console-header">
          <div>
            <p className="console-eyebrow">TYRAG / ENTERPRISE CONSOLE</p>
            <h1 className="mt-2 max-w-3xl text-3xl font-semibold tracking-[-0.05em] text-slate-950 sm:text-4xl">Gateway signal console</h1>
            <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-600">一个独立的公开 API 联调台：看见真实状态，保留认证边界，把服务、文件、会话和临时附件拆成互不拖垮的信号面。</p>
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <span className="console-mode-badge">{API_MODE === 'mock' ? 'TEST · MSW' : `${API_MODE.toUpperCase()} · PUBLIC API`}</span>
            <a href="/" className="console-secondary-button inline-flex items-center gap-1.5">返回业务界面 <ArrowUpRight size={13} /></a>
          </div>
        </header>

        <div className="console-signal-strip mt-5" aria-label="Console boundary">
          <span>public Gateway only</span><span>JWT user scope</span><span>HMAC producer stays server-side</span><span>no raw response replay</span>
        </div>

        <div className="mt-5 grid gap-4 lg:gap-5">
          <ServicePanel health={health} identity={identity} onRefresh={refreshAll} />
          <div className="grid gap-4 lg:grid-cols-2 lg:gap-5">
            <DocumentPanel state={documents} onRefresh={loadDocuments} />
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
          </div>
          <AttachmentPanel
            activeId={activeId}
            state={attachmentState}
            attachment={attachment}
            notice={attachmentNotice}
            onUpload={(file) => void uploadAttachment(file)}
            onIssueTicket={() => void issueTicket()}
            onDownload={() => void verifyDownload()}
          />
        </div>

        <footer className="mt-5 flex flex-wrap items-center justify-between gap-2 border-t border-slate-300/60 pt-4 text-[11px] text-slate-500">
          <span>Active trace: {activeSummary ? 'conversation selected' : 'no conversation selected'}</span>
          <span className="font-mono">route /console · Attachment contract v2.1.0</span>
        </footer>
      </div>
    </main>
  );
}
