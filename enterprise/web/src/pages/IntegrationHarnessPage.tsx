import { useCallback, useEffect, useMemo, useState } from 'react';
import { ErrorBanner } from '../components/errors/ErrorBanner';
import { ContextEditor } from '../components/harness/ContextEditor';
import { DocumentDiagnostics } from '../components/harness/DocumentDiagnostics';
import { DocumentEventForm } from '../components/harness/DocumentEventForm';
import { DocumentProducerNotice } from '../components/harness/DocumentProducerNotice';
import { HarnessChat } from '../components/harness/HarnessChat';
import { HarnessCitationPanel } from '../components/harness/HarnessCitationPanel';
import { TransientAttachmentPanel } from '../components/harness/TransientAttachmentPanel';
import { toDisplayError, getHarnessToken, setHarnessToken, v2Api } from '../api/v2Client';
import { API_MODE } from '../api/mode';
import { browserDocumentSyncEnabled } from '../api/documentSyncPolicy';
import type {
  Citation,
  ConversationDetail,
  ConversationSummary,
  DisplayError,
  DocumentCommand,
  DocumentOperation,
  PatchConversationContextRequest,
} from '../api/v2Types';
import { useV2Chat } from '../hooks/useV2Chat';

function isDocumentTerminal(status: string): boolean {
  return ['ready', 'failed', 'cancelled', 'superseded', 'disabled', 'deleted', 'review_required'].includes(status);
}

function summaryFromDetail(detail: ConversationDetail): ConversationSummary {
  return {
    conversationId: detail.conversationId,
    title: detail.title,
    status: detail.status,
    equipmentId: detail.equipmentId,
    fixedAssetNo: detail.fixedAssetNo,
    faultCode: detail.faultCode,
    contextVersion: detail.contextVersion,
    lastMessageAt: detail.lastMessageAt,
    createdAt: detail.createdAt,
  };
}

type DocumentQuery = Pick<DocumentCommand, 'externalDocumentId' | 'sourceVersionId'> &
  Partial<Pick<DocumentCommand, 'tenantId' | 'sourceSystem'>>;

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

export function IntegrationHarnessPage() {
  const browserDocumentSync = browserDocumentSyncEnabled(API_MODE);
  const [documentQuery, setDocumentQuery] = useState<DocumentQuery | null>(null);
  const [documentOperation, setDocumentOperation] = useState<DocumentOperation | null>(null);
  const [documentLoading, setDocumentLoading] = useState(false);
  const [documentError, setDocumentError] = useState<DisplayError | null>(null);
  const [documentItems, setDocumentItems] = useState<DocumentOperation[]>([]);
  const [documentListLoading, setDocumentListLoading] = useState(false);

  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationLoading, setConversationLoading] = useState(false);
  const [conversationError, setConversationError] = useState<DisplayError | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [activeConversation, setActiveConversation] = useState<ConversationDetail | null>(null);
  const [newEquipmentId, setNewEquipmentId] = useState(API_MODE === 'gateway' ? 'EQ-GD01250002' : 'EQ-1001');
  const [newFixedAssetNo, setNewFixedAssetNo] = useState(API_MODE === 'gateway' ? 'GD01250002' : 'FA-2001');
  const [newFaultCode, setNewFaultCode] = useState('E-104');
  const [contextSaving, setContextSaving] = useState(false);
  const [contextError, setContextError] = useState<DisplayError | null>(null);

  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);
  const [citationLoading, setCitationLoading] = useState(false);
  const [citationError, setCitationError] = useState<DisplayError | null>(null);
  const [attachmentLoading, setAttachmentLoading] = useState(false);
  const [attachmentError, setAttachmentError] = useState<DisplayError | null>(null);
  const [attachmentNotice, setAttachmentNotice] = useState<string | null>(null);
  const [tokenDraft, setTokenDraft] = useState('');
  const [tokenConfigured, setTokenConfigured] = useState(() => Boolean(getHarnessToken()));

  const chat = useV2Chat(activeId);

  const loadConversations = useCallback(async () => {
    setConversationLoading(true);
    setConversationError(null);
    try {
      const page = await v2Api.listConversations();
      setConversations(page.items);
      setActiveId((current) => current ?? page.items[0]?.conversationId ?? null);
    } catch (error) {
      setConversationError(toDisplayError(error));
    } finally {
      setConversationLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadConversations();
  }, [loadConversations]);

  useEffect(() => {
    if (!activeId) {
      setActiveConversation(null);
      return;
    }
    let cancelled = false;
    setConversationLoading(true);
    void v2Api
      .getConversation(activeId)
      .then((detail) => {
        if (!cancelled) setActiveConversation(detail);
      })
      .catch((error) => {
        if (!cancelled) setConversationError(toDisplayError(error));
      })
      .finally(() => {
        if (!cancelled) setConversationLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeId]);

  const refreshDocumentList = useCallback(async () => {
    setDocumentListLoading(true);
    try {
      const page = await v2Api.listDocumentStatus();
      setDocumentItems(page.items);
    } catch (error) {
      setDocumentError(toDisplayError(error));
    } finally {
      setDocumentListLoading(false);
    }
  }, []);

  const pollDocument = useCallback(async () => {
    if (!documentQuery) return;
    setDocumentLoading(true);
    try {
      const operation = await v2Api.getDocumentStatus(documentQuery.externalDocumentId, documentQuery);
      setDocumentOperation(operation);
    } catch (error) {
      setDocumentError(toDisplayError(error));
    } finally {
      setDocumentLoading(false);
    }
  }, [documentQuery]);

  useEffect(() => {
    if (!documentOperation || isDocumentTerminal(documentOperation.status)) return;
    const timer = window.setInterval(() => {
      void pollDocument();
    }, 2500);
    return () => window.clearInterval(timer);
  }, [documentOperation, pollDocument]);

  const submitDocument = useCallback(
    async (command: DocumentCommand) => {
      setDocumentLoading(true);
      setDocumentError(null);
      setDocumentQuery({
        externalDocumentId: command.externalDocumentId,
        sourceVersionId: command.sourceVersionId,
        tenantId: command.tenantId,
        sourceSystem: command.sourceSystem,
      });
      try {
        const operation = await v2Api.submitDocument(command);
        setDocumentOperation(operation);
        await refreshDocumentList();
      } catch (error) {
        setDocumentError(toDisplayError(error));
      } finally {
        setDocumentLoading(false);
      }
    },
    [refreshDocumentList],
  );

  const createConversation = useCallback(async () => {
    setConversationLoading(true);
    setConversationError(null);
    try {
      const detail = await v2Api.createConversation({
        equipmentId: newEquipmentId.trim() || null,
        fixedAssetNo: newFixedAssetNo.trim() || null,
        faultCode: newFaultCode.trim() || null,
      });
      setConversations((previous) => [summaryFromDetail(detail), ...previous]);
      setActiveId(detail.conversationId);
      setActiveConversation(detail);
    } catch (error) {
      setConversationError(toDisplayError(error));
    } finally {
      setConversationLoading(false);
    }
  }, [newEquipmentId, newFixedAssetNo, newFaultCode]);

  const selectConversation = useCallback((conversationId: string) => {
    setSelectedCitation(null);
    setCitationError(null);
    setAttachmentError(null);
    setAttachmentNotice(null);
    setActiveId(conversationId);
  }, []);

  const saveContext = useCallback(
    async (context: PatchConversationContextRequest) => {
      if (!activeId) return;
      setContextSaving(true);
      setContextError(null);
      try {
        const detail = await v2Api.patchConversationContext(activeId, context);
        setActiveConversation(detail);
        setConversations((previous) => previous.map((item) => item.conversationId === detail.conversationId ? summaryFromDetail(detail) : item));
      } catch (error) {
        setContextError(toDisplayError(error));
      } finally {
        setContextSaving(false);
      }
    },
    [activeId],
  );

  const selectCitation = useCallback(async (snapshot: Citation) => {
    setSelectedCitation(snapshot);
    setCitationLoading(true);
    setCitationError(null);
    try {
      const authorized = await v2Api.getCitation(snapshot.citationId);
      setSelectedCitation(authorized);
    } catch (error) {
      setCitationError(toDisplayError(error));
    } finally {
      setCitationLoading(false);
    }
  }, []);

  const uploadAttachment = useCallback(async (file: File) => {
    if (!activeId) return;
    setAttachmentLoading(true);
    setAttachmentError(null);
    setAttachmentNotice(null);
    try {
      await v2Api.createConversationAttachment(activeId, {
        fileName: file.name,
        mediaType: file.type || 'application/octet-stream',
        content: await encodeAttachment(file),
      });
      setAttachmentNotice('Gateway 已返回临时附件结果；附件不进入持久知识库。');
    } catch (error) {
      setAttachmentError(toDisplayError(error));
    } finally {
      setAttachmentLoading(false);
    }
  }, [activeId]);

  const visibleError = useMemo(
    () => chat.error ?? documentError ?? conversationError ?? contextError ?? citationError,
    [chat.error, documentError, conversationError, contextError, citationError],
  );

  return (
    <main data-testid="harness-page" className="min-h-screen bg-slate-100 text-slate-900">
      <div className="mx-auto max-w-[1500px] px-4 py-5 sm:px-6">
        <header className="mb-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-indigo-600">M1-E / T5 / WP-05</p>
              <h1 className="mt-1 text-xl font-semibold tracking-tight text-slate-900">TYrag v2 Integration Test Harness</h1>
              <p className="mt-1 max-w-3xl text-xs leading-5 text-slate-500">仅用于用户 JWT 会话、真 SSE、历史状态回放和契约诊断；不构成正式业务 UI。mock 模式文档区域仅用于 UI contract test，Gateway/demo 模式的文档同步由服务侧 producer 负责。</p>
            </div>
            <div className="flex items-center gap-2 text-xs">
              <span className="rounded-full bg-emerald-50 px-2.5 py-1 font-medium text-emerald-700">external contract v2.0.0</span>
              <span data-testid="harness-api-mode" className="rounded-full bg-slate-100 px-2.5 py-1 font-medium text-slate-600">
                {API_MODE === 'gateway' ? 'Gateway v2 用户 Harness · 文档 producer 独立' : `UI contract ${API_MODE}（非 Integration）`}
              </span>
              <span className={`rounded-full px-2.5 py-1 font-medium ${tokenConfigured ? 'bg-slate-100 text-slate-700' : 'bg-amber-50 text-amber-700'}`}>{tokenConfigured ? 'Bearer 已注入' : '无 Bearer（可测试 401）'}</span>
            </div>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
            <label className="text-xs text-slate-500" htmlFor="harness-token">运行期 Bearer（不写入源码）</label>
            <input id="harness-token" type="password" value={tokenDraft} onChange={(event) => setTokenDraft(event.target.value)} placeholder={tokenConfigured ? '已配置，可留空' : '仅本地联调注入'} className="w-64 rounded-md border border-slate-200 px-2.5 py-1.5 text-xs" />
            <button type="button" onClick={() => { setHarnessToken(tokenDraft); setTokenDraft(''); setTokenConfigured(Boolean(getHarnessToken())); }} className="rounded-md border border-slate-200 px-2.5 py-1.5 text-xs text-slate-600 hover:bg-slate-50">保存运行期凭据</button>
            <span className="text-[11px] text-slate-400">不显示、不记录 Token。</span>
          </div>
        </header>

        {visibleError && <ErrorBanner error={visibleError} onDismiss={() => {}} />}

        <div data-testid="harness-layout" className="grid grid-cols-1 gap-4 xl:grid-cols-[360px_minmax(0,1fr)_300px]">
          <aside className="space-y-4">
            <section aria-label="Asset Registry 设备选择" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
              <div className="flex items-center justify-between gap-2">
                <div><h2 className="text-sm font-semibold text-slate-800">Asset Registry 设备选择</h2><p className="mt-1 text-[11px] text-slate-500">v2 cursor page · owned sessions</p></div>
                <button type="button" onClick={() => void loadConversations()} className="rounded-md border border-slate-200 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50">刷新</button>
              </div>
              <div className="mt-3 space-y-1.5">
                {conversationLoading && <p className="text-xs text-slate-400">加载中…</p>}
                {!conversationLoading && conversations.length === 0 && <p className="text-xs text-slate-400">暂无会话</p>}
                {conversations.map((conversation) => (
                  <button type="button" key={conversation.conversationId} onClick={() => selectConversation(conversation.conversationId)} className={`w-full rounded-lg border px-3 py-2 text-left ${conversation.conversationId === activeId ? 'border-indigo-300 bg-indigo-50' : 'border-slate-100 hover:bg-slate-50'}`}>
                    <p className="truncate text-xs font-medium text-slate-800">{conversation.title}</p>
                    <p className="mt-1 text-[10px] text-slate-500">{conversation.equipmentId ?? '未绑定 Asset'} · v{conversation.contextVersion}</p>
                  </button>
                ))}
              </div>
              <div className="mt-4 border-t border-slate-100 pt-3">
                <p className="mb-1 text-xs font-medium text-slate-700">选择设备并创建会话</p>
                <p className="mb-2 text-[11px] leading-5 text-slate-500">equipmentId/fixedAssetNo 仅作为 Registry 查询键；canonical snapshot 由 Gateway 返回。本地联调请使用 <code>EQ-GD01250002</code> + <code>GD01250002</code>（或 <code>EQ-GR01220020</code> + <code>GR01220020</code>）。</p>
                <div className="space-y-2">
                  <input aria-label="new equipmentId" value={newEquipmentId} onChange={(event) => setNewEquipmentId(event.target.value)} placeholder="equipmentId，例如 EQ-GD01250002" className="w-full rounded-md border border-slate-200 px-2.5 py-2 text-xs" />
                  <input aria-label="new fixedAssetNo" value={newFixedAssetNo} onChange={(event) => setNewFixedAssetNo(event.target.value)} placeholder="fixedAssetNo，例如 GD01250002" className="w-full rounded-md border border-slate-200 px-2.5 py-2 text-xs" />
                  <input aria-label="new faultCode" value={newFaultCode} onChange={(event) => setNewFaultCode(event.target.value)} placeholder="faultCode，例如 E-104" className="w-full rounded-md border border-slate-200 px-2.5 py-2 text-xs" />
                  <button type="button" onClick={() => void createConversation()} disabled={conversationLoading} className="w-full rounded-md bg-slate-800 px-3 py-2 text-xs font-medium text-white hover:bg-slate-900 disabled:opacity-50">创建并选择</button>
                </div>
              </div>
            </section>
            <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
              <h2 className="text-sm font-semibold text-slate-800">Asset context 切换</h2>
              <p className="mt-1 mb-3 text-[11px] text-slate-500">PATCH /conversations/{activeId ?? '…'}/context</p>
              <ContextEditor conversation={activeConversation} saving={contextSaving} error={contextError} onSave={(context) => void saveContext(context)} />
            </section>
          </aside>

          <div className="space-y-4">
            <HarnessChat conversation={activeConversation} messages={chat.messages} isStreaming={chat.isStreaming} error={chat.error} onSend={chat.sendMessage} onRetry={chat.retry} onCancel={chat.cancelStream} onCitation={(citation) => void selectCitation(citation)} />
            <TransientAttachmentPanel conversationId={activeId} loading={attachmentLoading} error={attachmentError} notice={attachmentNotice} onUpload={(file) => void uploadAttachment(file)} />
            {browserDocumentSync && <DocumentDiagnostics operation={documentOperation} loading={documentLoading} error={documentError} onRefresh={() => void pollDocument()} />}
          </div>

          <aside className="space-y-4">
             {browserDocumentSync ? <>
               <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
                 <div className="mb-3 flex items-center justify-between gap-2"><div><h2 className="text-sm font-semibold text-slate-800">文件事件</h2><p className="mt-1 text-[11px] text-slate-500">mock POST /documents · 非 Integration 证据</p></div><button type="button" onClick={() => void refreshDocumentList()} className="rounded-md border border-slate-200 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50">{documentListLoading ? '查询中…' : '列表'}</button></div>
                 <DocumentEventForm loading={documentLoading} onSubmit={(command) => void submitDocument(command)} />
               </section>
               <section aria-label="文件操作列表" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
                 <h2 className="text-sm font-semibold text-slate-800">最近文件操作（mock）</h2>
                 <div className="mt-3 space-y-2">
                   {documentItems.length === 0 && <p className="text-xs text-slate-400">尚未加载列表。</p>}
                   {documentItems.map((item) => <button type="button" key={item.operationId} onClick={() => { setDocumentOperation(item); setDocumentQuery({ externalDocumentId: item.externalDocumentId, sourceVersionId: item.sourceVersionId }); }} className="w-full rounded-md border border-slate-100 px-2.5 py-2 text-left text-xs hover:bg-slate-50"><span className="font-medium text-slate-800">{item.externalDocumentId}</span><span className="ml-2 text-slate-500">{item.status} · {item.stage}</span></button>)}
                 </div>
               </section>
             </> : <DocumentProducerNotice />}
            <HarnessCitationPanel citation={selectedCitation} loading={citationLoading} error={citationError} onClose={() => { setSelectedCitation(null); setCitationError(null); }} />
          </aside>
        </div>
      </div>
    </main>
  );
}
