import { useCallback, useEffect, useMemo, useState } from 'react';
import { ErrorBanner } from '../components/errors/ErrorBanner';
import { HarnessChat } from '../components/harness/HarnessChat';
import { HarnessContextBar } from '../components/harness/HarnessContextBar';
import { HarnessCitationPanel } from '../components/harness/HarnessCitationPanel';
import { GatewayRuntimeLog } from '../components/harness/GatewayRuntimeLog';
import { ConsoleOverlay } from '../components/console/ConsoleOverlay';
import { DEFAULT_PAGE_SIZE, PaginationBar } from '../components/console/ConsoleTableControls';
import { WorkbenchShell, useWorkbenchTab } from '../components/layout/WorkbenchShell';
import { toDisplayError, v2Api } from '../api/v2Client';
import { MessageCircle } from 'lucide-react';
import type {
  Citation,
  ConversationDetail,
  ConversationSummary,
  DisplayError,
  PatchConversationContextRequest,
  ReasoningMode,
} from '../api/v2Types';
import { useV2Chat } from '../hooks/useV2Chat';
import './enterprise-console.css';

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

const HARNESS_TABS = ['ask', 'runtime'] as const;
type HarnessTab = (typeof HARNESS_TABS)[number];

const HARNESS_NAV = [
  {
    id: 'integration',
    label: '联调',
    items: [{ id: 'ask', label: '问答会话' }],
  },
  {
    id: 'ops',
    label: '运行',
    items: [{ id: 'runtime', label: 'HTTP 日志' }],
  },
];

const CONVERSATION_PAGE_SIZE = DEFAULT_PAGE_SIZE;

function formatConversationTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '未提供';
  const pad = (part: number) => String(part).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function IntegrationHarnessPage() {
  const [tab, setTab] = useWorkbenchTab<HarnessTab>('ask', HARNESS_TABS);

  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationLoading, setConversationLoading] = useState(false);
  const [conversationError, setConversationError] = useState<DisplayError | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [conversationPage, setConversationPage] = useState(1);
  const [conversationPageSize, setConversationPageSize] = useState(CONVERSATION_PAGE_SIZE);
  const [conversationRequestCursor, setConversationRequestCursor] = useState<string | null>(null);
  const [conversationNextCursor, setConversationNextCursor] = useState<string | null>(null);
  const [conversationHasMore, setConversationHasMore] = useState(false);
  const [conversationCursorStack, setConversationCursorStack] = useState<Array<string | null>>([]);
  const [activeConversation, setActiveConversation] = useState<ConversationDetail | null>(null);
  const [showDeviceCreate, setShowDeviceCreate] = useState(false);
  const [newEquipmentId, setNewEquipmentId] = useState('');
  const [contextSaving, setContextSaving] = useState(false);
  const [contextError, setContextError] = useState<DisplayError | null>(null);

  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);
  const [selectedCitationGroup, setSelectedCitationGroup] = useState<Citation[]>([]);
  const [citationLoading, setCitationLoading] = useState(false);
  const [citationError, setCitationError] = useState<DisplayError | null>(null);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [reasoningMode, setReasoningMode] = useState<ReasoningMode>('simple');
  const [internetEnabled, setInternetEnabled] = useState(false);

  const chat = useV2Chat(activeId, { reasoningMode, internetEnabled });

  const loadConversations = useCallback(async (cursor: string | null = null, pageNumber = 1) => {
    setConversationLoading(true);
    setConversationError(null);
    try {
      const page = await v2Api.listConversations({
        limit: conversationPageSize,
        ...(cursor ? { cursor } : {}),
      });
      setConversations(page.items);
      setConversationRequestCursor(cursor);
      setConversationNextCursor(page.nextCursor);
      setConversationHasMore(page.hasMore);
      setConversationPage(pageNumber);
      setActiveId((current) => current ?? page.items[0]?.conversationId ?? null);
    } catch (error) {
      setConversationError(toDisplayError(error));
    } finally {
      setConversationLoading(false);
    }
  }, [conversationPageSize]);

  const refreshConversations = useCallback(() => {
    void loadConversations(conversationRequestCursor, conversationPage);
  }, [conversationPage, conversationRequestCursor, loadConversations]);

  const nextConversationPage = useCallback(() => {
    if (!conversationNextCursor || conversationLoading) return;
    setConversationCursorStack((previous) => [...previous, conversationRequestCursor]);
    void loadConversations(conversationNextCursor, conversationPage + 1);
  }, [conversationLoading, conversationNextCursor, conversationPage, conversationRequestCursor, loadConversations]);

  const previousConversationPage = useCallback(() => {
    if (conversationCursorStack.length === 0 || conversationLoading) return;
    const previousCursor = conversationCursorStack[conversationCursorStack.length - 1] ?? null;
    setConversationCursorStack((previous) => previous.slice(0, -1));
    void loadConversations(previousCursor, Math.max(1, conversationPage - 1));
  }, [conversationCursorStack, conversationLoading, conversationPage, loadConversations]);

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

  useEffect(() => {
    // Staged question files belong to a single conversation; drop them on switch.
    setPendingFiles([]);
  }, [activeId]);

  useEffect(() => {
    if (!showDeviceCreate) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setShowDeviceCreate(false);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [showDeviceCreate]);

  const createConversation = useCallback(
    async (context?: { equipmentId?: string | null; fixedAssetNo?: string | null }): Promise<ConversationDetail | null> => {
      setConversationLoading(true);
      setConversationError(null);
      try {
        // Equipment fields are optional in contract v2: without them the run
        // searches the tenant-visible corpus and reminds the user to bind a device.
        const detail = await v2Api.createConversation(context ?? {});
        setConversations((previous) => [summaryFromDetail(detail), ...previous]);
        setActiveId(detail.conversationId);
        setActiveConversation(detail);
        return detail;
      } catch (error) {
        setConversationError(toDisplayError(error));
        return null;
      } finally {
        setConversationLoading(false);
      }
    },
    [],
  );

  const createConversationWithDevice = useCallback(async () => {
    const detail = await createConversation({
      equipmentId: newEquipmentId.trim() || null,
    });
    if (detail) {
      setShowDeviceCreate(false);
      setNewEquipmentId('');
    }
  }, [createConversation, newEquipmentId]);

  const selectConversation = useCallback((conversationId: string) => {
    setSelectedCitation(null);
    setCitationError(null);
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

  const selectCitationGroup = useCallback((citations: Citation[]) => {
    setSelectedCitationGroup(citations);
    if (citations[0]) void selectCitation(citations[0]);
  }, [selectCitation]);

  const closeCitation = useCallback(() => {
    setSelectedCitation(null);
    setSelectedCitationGroup([]);
    setCitationError(null);
  }, []);

  const addPendingFiles = useCallback((files: File[]) => {
    setPendingFiles((previous) => [...previous, ...files]);
  }, []);

  const removePendingFile = useCallback((index: number) => {
    setPendingFiles((previous) => previous.filter((_, itemIndex) => itemIndex !== index));
  }, []);

  const sendWithFiles = useCallback(
    (question: string) => {
      // Only clear the staged files when the send was accepted; validation
      // failures keep them staged so the user can fix and resend directly.
      if (chat.sendMessage(question, pendingFiles)) {
        setPendingFiles([]);
      }
    },
    [chat, pendingFiles],
  );

  const visibleError = useMemo(
    () => chat.error ?? conversationError ?? contextError ?? citationError,
    [chat.error, conversationError, contextError, citationError],
  );

  const citationDrawerOpen = selectedCitation !== null || citationError !== null;

  return (
    <WorkbenchShell
      testId="harness-page"
      shellClass="harness-shell"
      brand="Harness"
      subtitle="M1-E / T5 / WP-05"
      groups={HARNESS_NAV}
      activeId={tab}
      onSelect={(id) => setTab(id as HarnessTab)}
      actions={null}
      tokenRow={null}
      footer={(
        <footer className="console-footer">
          <span>Active session: {activeConversation ? activeConversation.title : 'no conversation selected'}</span>
          <span className="console-route">route /</span>
          <span className="console-route">external contract v2.9.0</span>
        </footer>
      )}
    >
      {visibleError && !showDeviceCreate && <ErrorBanner error={visibleError} onDismiss={() => {}} />}

      {tab === 'ask' && (
        <div data-testid="harness-layout" className="harness-layout">
          <aside className="harness-stack">
            <section aria-label="会话管理" className="console-card">
              <div className="console-card-head">
                <div>
                  <p className="console-eyebrow">Sessions</p>
                  <h2>会话</h2>
                  <p>Gateway v2 会话记录 · 第 {conversationPage} 页</p>
                </div>
                <div className="console-card-actions">
                  <button type="button" onClick={refreshConversations} className="console-secondary-button">刷新</button>
                  <button type="button" onClick={() => void createConversation()} disabled={conversationLoading} className="console-primary-button">+ 新建会话</button>
                </div>
              </div>
              <div className="console-card-body harness-session-body">
                <div className="harness-session-list">
                  {conversationLoading && <p className="console-empty">加载中…</p>}
                  {!conversationLoading && conversations.length === 0 && <p className="console-empty">暂无会话，点击「+ 新建会话」开始。</p>}
                  {conversations.map((conversation) => (
                    <button type="button" key={conversation.conversationId} onClick={() => selectConversation(conversation.conversationId)} className={`console-list-btn ${conversation.conversationId === activeId ? 'is-active' : ''}`}>
                      <div className="harness-session-item-head">
                        <p>{conversation.title}</p>
                        <time dateTime={conversation.lastMessageAt}>{formatConversationTime(conversation.lastMessageAt)}</time>
                      </div>
                      <p className="console-route">设备: {conversation.equipmentId ?? '未绑定设备'}</p>
                    </button>
                  ))}
                </div>
                <PaginationBar
                  page={conversationPage}
                  itemCount={conversations.length}
                  hasMore={conversationHasMore && !conversationLoading}
                  pageSize={conversationPageSize}
                  onPageSizeChange={(value) => {
                    setConversationPageSize(value);
                    setConversationCursorStack([]);
                    setConversationRequestCursor(null);
                    setConversationPage(1);
                  }}
                  onPrevious={previousConversationPage}
                  onNext={nextConversationPage}
                  label="条"
                />
                <div className="console-pad harness-device-create">
                  <button
                    type="button"
                    onClick={() => setShowDeviceCreate(true)}
                    aria-expanded={showDeviceCreate}
                    aria-haspopup="dialog"
                    className="console-secondary-button is-full"
                  >
                    <MessageCircle size={16} aria-hidden="true" />
                    指定设备创建（可选）
                  </button>
                </div>
              </div>
            </section>
          </aside>

          <div className="harness-conversation-panel">
            <HarnessContextBar conversation={activeConversation} saving={contextSaving} error={contextError} onSave={(context) => void saveContext(context)} />
            <HarnessChat
              conversation={activeConversation}
              messages={chat.messages}
              isStreaming={chat.isStreaming}
              error={chat.error}
              onSend={sendWithFiles}
              onRetry={chat.retry}
              onCancel={chat.cancelStream}
              onCitation={(citation) => { setSelectedCitationGroup([]); void selectCitation(citation); }}
              onCitationGroup={selectCitationGroup}
              selectedFiles={pendingFiles}
              onFilesPicked={addPendingFiles}
              onRemoveFile={removePendingFile}
              reasoningMode={reasoningMode}
              onReasoningModeChange={setReasoningMode}
              internetEnabled={internetEnabled}
              onInternetEnabledChange={setInternetEnabled}
            />
          </div>
        </div>
      )}

      <ConsoleOverlay
        open={showDeviceCreate}
        mode="dialog"
        onClose={() => setShowDeviceCreate(false)}
        ariaLabel="指定设备创建"
        className="harness-device-modal"
      >
          <form
            className="harness-device-modal-form"
            onSubmit={(event) => {
              event.preventDefault();
              void createConversationWithDevice();
            }}
          >
            <div className="harness-device-modal-head">
              <div>
                <p className="console-eyebrow">可选上下文</p>
                <h2>指定设备创建</h2>
                <p>为新会话预先绑定设备，后续检索会优先限定在该设备资料内。</p>
              </div>
              <button type="button" className="console-icon-button" aria-label="关闭指定设备创建" onClick={() => setShowDeviceCreate(false)}>×</button>
            </div>
            <div className="harness-device-modal-body">
              <label className="diag-field">
                设备编号 <small>equipmentId</small>
                <input aria-label="new equipmentId" value={newEquipmentId} onChange={(event) => setNewEquipmentId(event.target.value)} placeholder="可留空" className="diag-input" autoFocus />
              </label>
              <p className="diag-help">设备号可留空：不绑定时在当前用户可见文档内全局检索，回答末尾会提示补充设备号。</p>
              {conversationError && <ErrorBanner error={conversationError} onDismiss={() => {}} />}
            </div>
            <div className="harness-device-modal-actions">
              <button type="button" className="console-secondary-button" onClick={() => setShowDeviceCreate(false)}>取消</button>
              <button type="submit" disabled={conversationLoading} className="console-primary-button">{conversationLoading ? '创建中…' : '创建会话'}</button>
            </div>
          </form>
      </ConsoleOverlay>

      {tab === 'runtime' && <GatewayRuntimeLog />}

      <ConsoleOverlay
        open={citationDrawerOpen}
        mode="dialog"
        onClose={closeCitation}
        ariaLabel="citation 抽屉"
        className="harness-citation-overlay"
      >
        <HarnessCitationPanel
          citation={selectedCitation}
          citations={selectedCitationGroup}
          loading={citationLoading}
          error={citationError}
          onClose={closeCitation}
          onSelectCitation={(citation) => void selectCitation(citation)}
        />
      </ConsoleOverlay>
    </WorkbenchShell>
  );
}
