import { useCallback, useEffect, useMemo, useState } from 'react';
import { ErrorBanner } from '../components/errors/ErrorBanner';
import { ContextEditor } from '../components/harness/ContextEditor';
import { DocumentDiagnostics } from '../components/harness/DocumentDiagnostics';
import { DocumentEventForm } from '../components/harness/DocumentEventForm';
import { DocumentProducerNotice } from '../components/harness/DocumentProducerNotice';
import { HarnessChat } from '../components/harness/HarnessChat';
import { HarnessCitationPanel } from '../components/harness/HarnessCitationPanel';
import { TransientAttachmentPanel } from '../components/harness/TransientAttachmentPanel';
import { GatewayRuntimeLog } from '../components/harness/GatewayRuntimeLog';
import { WorkbenchShell, useWorkbenchTab } from '../components/layout/WorkbenchShell';
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
import './enterprise-console.css';

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

const HARNESS_TABS = ['ask', 'attachment', 'documents', 'runtime'] as const;
type HarnessTab = (typeof HARNESS_TABS)[number];

const HARNESS_NAV = [
  {
    id: 'integration',
    label: '联调',
    items: [
      { id: 'ask', label: '问答会话' },
      { id: 'attachment', label: '临时附件' },
      { id: 'documents', label: '文档' },
    ],
  },
  {
    id: 'ops',
    label: '运行',
    items: [{ id: 'runtime', label: 'HTTP 日志' }],
  },
];

export function IntegrationHarnessPage() {
  const browserDocumentSync = browserDocumentSyncEnabled(API_MODE);
  const [tab, setTab] = useWorkbenchTab<HarnessTab>('ask', HARNESS_TABS);
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
    <WorkbenchShell
      testId="harness-page"
      shellClass="harness-shell"
      brand="Harness"
      subtitle="M1-E / T5 / WP-05"
      groups={HARNESS_NAV}
      activeId={tab}
      onSelect={(id) => setTab(id as HarnessTab)}
      actions={(
        <>
          <span className="console-mode-badge">external contract v2.0.0</span>
          <span data-testid="harness-api-mode" className="console-mode-badge">
            {API_MODE === 'gateway' ? 'Gateway v2 用户 Harness · 文档 producer 独立' : `UI contract ${API_MODE}（非 Integration）`}
          </span>
          <span className="console-mode-badge">{tokenConfigured ? 'Bearer 已注入' : '无 Bearer（可测试 401）'}</span>
          <a href="/console" className="console-secondary-button">打开联调 Console</a>
        </>
      )}
      tokenRow={(
        <div className="console-token-row">
          <label htmlFor="harness-token">运行期 Bearer（不写入源码）</label>
          <input id="harness-token" type="password" value={tokenDraft} onChange={(event) => setTokenDraft(event.target.value)} placeholder={tokenConfigured ? '已配置，可留空' : '仅本地联调注入'} />
          <button type="button" onClick={() => { setHarnessToken(tokenDraft); setTokenDraft(''); setTokenConfigured(Boolean(getHarnessToken())); }} className="console-primary-button">保存运行期凭据</button>
          <span className="console-route">不显示、不记录 Token。</span>
        </div>
      )}
      footer={(
        <footer className="console-footer">
          <span>Active session: {activeConversation ? activeConversation.title : 'no conversation selected'}</span>
          <span className="console-route">route / · Attachment contract v2.1.0</span>
        </footer>
      )}
    >
      {visibleError && <ErrorBanner error={visibleError} onDismiss={() => {}} />}

      {tab === 'ask' && (
        <div data-testid="harness-layout" className="harness-layout">
          <aside className="harness-stack">
            <section aria-label="Asset Registry 设备选择" className="console-card">
              <div className="console-card-head">
                <div>
                  <p className="console-eyebrow">Sessions</p>
                  <h2>Asset Registry 设备选择</h2>
                  <p>v2 cursor page · owned sessions</p>
                </div>
                <div className="console-card-actions">
                  <button type="button" onClick={() => void loadConversations()} className="console-secondary-button">刷新</button>
                </div>
              </div>
              <div className="console-card-body">
                {conversationLoading && <p className="console-empty">加载中…</p>}
                {!conversationLoading && conversations.length === 0 && <p className="console-empty">暂无会话</p>}
                {conversations.map((conversation) => (
                  <button type="button" key={conversation.conversationId} onClick={() => selectConversation(conversation.conversationId)} className={`console-list-btn ${conversation.conversationId === activeId ? 'is-active' : ''}`}>
                    <p>{conversation.title}</p>
                    <p className="console-route">{conversation.equipmentId ?? '未绑定 Asset'} · v{conversation.contextVersion}</p>
                  </button>
                ))}
                <div className="console-note">
                  <p><strong>选择设备并创建会话</strong></p>
                  <p>equipmentId/fixedAssetNo 仅作为 Registry 查询键；canonical snapshot 由 Gateway 返回。本地联调请使用 <code>EQ-GD01250002</code> + <code>GD01250002</code>（或 <code>EQ-GR01220020</code> + <code>GR01220020</code>）。</p>
                </div>
                <div className="console-pad">
                  <div className="harness-stack">
                    <input aria-label="new equipmentId" value={newEquipmentId} onChange={(event) => setNewEquipmentId(event.target.value)} placeholder="equipmentId，例如 EQ-GD01250002" className="diag-input" />
                    <input aria-label="new fixedAssetNo" value={newFixedAssetNo} onChange={(event) => setNewFixedAssetNo(event.target.value)} placeholder="fixedAssetNo，例如 GD01250002" className="diag-input" />
                    <input aria-label="new faultCode" value={newFaultCode} onChange={(event) => setNewFaultCode(event.target.value)} placeholder="faultCode，例如 E-104" className="diag-input" />
                    <button type="button" onClick={() => void createConversation()} disabled={conversationLoading} className="console-primary-button is-full">创建并选择</button>
                  </div>
                </div>
              </div>
            </section>
            <section className="console-card">
              <div className="console-card-head">
                <div>
                  <p className="console-eyebrow">Context</p>
                  <h2>Asset context 切换</h2>
                  <p>PATCH /conversations/{activeId ?? '…'}/context</p>
                </div>
              </div>
              <div className="console-card-body">
                <div className="console-pad">
                  <ContextEditor conversation={activeConversation} saving={contextSaving} error={contextError} onSave={(context) => void saveContext(context)} />
                </div>
              </div>
            </section>
          </aside>

          <div className="harness-stack">
            <HarnessChat conversation={activeConversation} messages={chat.messages} isStreaming={chat.isStreaming} error={chat.error} onSend={chat.sendMessage} onRetry={chat.retry} onCancel={chat.cancelStream} onCitation={(citation) => void selectCitation(citation)} />
          </div>

          <aside className="harness-stack">
            <HarnessCitationPanel citation={selectedCitation} loading={citationLoading} error={citationError} onClose={() => { setSelectedCitation(null); setCitationError(null); }} />
          </aside>
        </div>
      )}

      {tab === 'attachment' && (
        <TransientAttachmentPanel conversationId={activeId} loading={attachmentLoading} error={attachmentError} notice={attachmentNotice} onUpload={(file) => void uploadAttachment(file)} />
      )}

      {tab === 'documents' && (
        <div className="harness-stack">
          {browserDocumentSync ? (
            <>
              <section className="console-card">
                <div className="console-card-head">
                  <div>
                    <p className="console-eyebrow">Documents</p>
                    <h2>文件事件</h2>
                    <p>mock POST /documents · 非 Integration 证据</p>
                  </div>
                  <div className="console-card-actions">
                    <button type="button" onClick={() => void refreshDocumentList()} className="console-secondary-button">{documentListLoading ? '查询中…' : '列表'}</button>
                  </div>
                </div>
                <div className="console-card-body">
                  <div className="console-pad">
                    <DocumentEventForm loading={documentLoading} onSubmit={(command) => void submitDocument(command)} />
                  </div>
                </div>
              </section>
              <section aria-label="文件操作列表" className="console-card">
                <div className="console-card-head">
                  <div>
                    <p className="console-eyebrow">Recent</p>
                    <h2>最近文件操作（mock）</h2>
                  </div>
                </div>
                <div className="console-card-body">
                  {documentItems.length === 0 && <p className="console-empty">尚未加载列表。</p>}
                  {documentItems.map((item) => (
                    <button type="button" key={item.operationId} onClick={() => { setDocumentOperation(item); setDocumentQuery({ externalDocumentId: item.externalDocumentId, sourceVersionId: item.sourceVersionId }); }} className="console-list-btn">
                      <p>{item.externalDocumentId}</p>
                      <p className="console-route">{item.status} · {item.stage}</p>
                    </button>
                  ))}
                </div>
              </section>
              <DocumentDiagnostics operation={documentOperation} loading={documentLoading} error={documentError} onRefresh={() => void pollDocument()} />
            </>
          ) : (
            <DocumentProducerNotice />
          )}
        </div>
      )}

      {tab === 'runtime' && <GatewayRuntimeLog />}
    </WorkbenchShell>
  );
}
