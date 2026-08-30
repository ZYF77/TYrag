import { useCallback, useEffect, useMemo, useState } from 'react';
import { ErrorBanner } from '../components/errors/ErrorBanner';
import { HarnessChat } from '../components/harness/HarnessChat';
import { HarnessContextBar } from '../components/harness/HarnessContextBar';
import { HarnessCitationPanel } from '../components/harness/HarnessCitationPanel';
import { GatewayRuntimeLog } from '../components/harness/GatewayRuntimeLog';
import { WorkbenchShell, useWorkbenchTab } from '../components/layout/WorkbenchShell';
import { toDisplayError, getHarnessToken, setHarnessToken, v2Api } from '../api/v2Client';
import { API_MODE } from '../api/mode';
import type {
  Citation,
  ConversationDetail,
  ConversationSummary,
  DisplayError,
  PatchConversationContextRequest,
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

const EXTERNAL_CONTRACT_BADGE = 'external contract v2.9.0';

export function IntegrationHarnessPage() {
  const [tab, setTab] = useWorkbenchTab<HarnessTab>('ask', HARNESS_TABS);

  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationLoading, setConversationLoading] = useState(false);
  const [conversationError, setConversationError] = useState<DisplayError | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [activeConversation, setActiveConversation] = useState<ConversationDetail | null>(null);
  const [showDeviceCreate, setShowDeviceCreate] = useState(false);
  const [newEquipmentId, setNewEquipmentId] = useState('');
  const [newFixedAssetNo, setNewFixedAssetNo] = useState('');
  const [contextSaving, setContextSaving] = useState(false);
  const [contextError, setContextError] = useState<DisplayError | null>(null);

  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);
  const [citationLoading, setCitationLoading] = useState(false);
  const [citationError, setCitationError] = useState<DisplayError | null>(null);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
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

  useEffect(() => {
    // Staged question files belong to a single conversation; drop them on switch.
    setPendingFiles([]);
  }, [activeId]);

  const createConversation = useCallback(
    async (context?: { equipmentId?: string | null; fixedAssetNo?: string | null }) => {
      setConversationLoading(true);
      setConversationError(null);
      try {
        // Equipment fields are optional in contract v2: without them the run
        // searches the tenant-visible corpus and reminds the user to bind a device.
        const detail = await v2Api.createConversation(context ?? {});
        setConversations((previous) => [summaryFromDetail(detail), ...previous]);
        setActiveId(detail.conversationId);
        setActiveConversation(detail);
      } catch (error) {
        setConversationError(toDisplayError(error));
      } finally {
        setConversationLoading(false);
      }
    },
    [],
  );

  const createConversationWithDevice = useCallback(async () => {
    await createConversation({
      equipmentId: newEquipmentId.trim() || null,
      fixedAssetNo: newFixedAssetNo.trim() || null,
    });
  }, [createConversation, newEquipmentId, newFixedAssetNo]);

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

  const closeCitation = useCallback(() => {
    setSelectedCitation(null);
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
      actions={(
        <>
          <span className="console-mode-badge">{EXTERNAL_CONTRACT_BADGE}</span>
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
          <span className="console-route">route /</span>
        </footer>
      )}
    >
      {visibleError && <ErrorBanner error={visibleError} onDismiss={() => {}} />}

      {tab === 'ask' && (
        <div data-testid="harness-layout" className="harness-layout">
          <aside className="harness-stack">
            <section aria-label="会话管理" className="console-card">
              <div className="console-card-head">
                <div>
                  <p className="console-eyebrow">Sessions</p>
                  <h2>会话</h2>
                  <p>v2 cursor page · owned sessions</p>
                </div>
                <div className="console-card-actions">
                  <button type="button" onClick={() => void loadConversations()} className="console-secondary-button">刷新</button>
                  <button type="button" onClick={() => void createConversation()} disabled={conversationLoading} className="console-primary-button">+ 新建会话</button>
                </div>
              </div>
              <div className="console-card-body">
                {conversationLoading && <p className="console-empty">加载中…</p>}
                {!conversationLoading && conversations.length === 0 && <p className="console-empty">暂无会话，点击「+ 新建会话」开始。</p>}
                {conversations.map((conversation) => (
                  <button type="button" key={conversation.conversationId} onClick={() => selectConversation(conversation.conversationId)} className={`console-list-btn ${conversation.conversationId === activeId ? 'is-active' : ''}`}>
                    <p>{conversation.title}</p>
                    <p className="console-route">设备: {conversation.equipmentId ?? '未绑定设备'}</p>
                  </button>
                ))}
                <div className="console-pad harness-device-create">
                  <button
                    type="button"
                    onClick={() => setShowDeviceCreate((previous) => !previous)}
                    aria-expanded={showDeviceCreate}
                    className="console-secondary-button is-full"
                  >
                    指定设备创建（可选）
                  </button>
                  {showDeviceCreate && (
                    <div className="harness-stack">
                      <input aria-label="new equipmentId" value={newEquipmentId} onChange={(event) => setNewEquipmentId(event.target.value)} placeholder="equipmentId（可留空）" className="diag-input" />
                      <input aria-label="new fixedAssetNo" value={newFixedAssetNo} onChange={(event) => setNewFixedAssetNo(event.target.value)} placeholder="fixedAssetNo（可留空）" className="diag-input" />
                      <p className="diag-help">设备号可留空：不绑定时在当前用户可见文档内全局检索，回答末尾会提示补充设备号。</p>
                      <button type="button" onClick={() => void createConversationWithDevice()} disabled={conversationLoading} className="console-primary-button is-full">创建会话</button>
                    </div>
                  )}
                </div>
              </div>
            </section>
          </aside>

          <div className="harness-stack">
            <HarnessContextBar conversation={activeConversation} saving={contextSaving} error={contextError} onSave={(context) => void saveContext(context)} />
            <HarnessChat
              conversation={activeConversation}
              messages={chat.messages}
              isStreaming={chat.isStreaming}
              error={chat.error}
              onSend={sendWithFiles}
              onRetry={chat.retry}
              onCancel={chat.cancelStream}
              onCitation={(citation) => void selectCitation(citation)}
              selectedFiles={pendingFiles}
              onFilesPicked={addPendingFiles}
              onRemoveFile={removePendingFile}
            />
          </div>
        </div>
      )}

      {tab === 'runtime' && <GatewayRuntimeLog />}

      {citationDrawerOpen && (
        <div className="harness-drawer-layer">
          <div className="harness-drawer-backdrop" onClick={closeCitation} aria-hidden="true" />
          <aside className="harness-drawer" role="dialog" aria-label="citation 抽屉">
            <HarnessCitationPanel citation={selectedCitation} loading={citationLoading} error={citationError} onClose={closeCitation} />
          </aside>
        </div>
      )}
    </WorkbenchShell>
  );
}
