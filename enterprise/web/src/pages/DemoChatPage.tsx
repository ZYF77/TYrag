import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { demoApi, getDemoToken, setDemoToken } from '../api/demoClient';
import { normalizeError } from '../api/errors';
import type {
  Citation,
  Conversation,
  DemoDocumentStatus,
  ErrorResponse,
} from '../api/types';
import { ChatArea } from '../components/chat/ChatArea';
import { CitationDrawer } from '../components/citations/CitationDrawer';
import { DemoDocumentStatusCard } from '../components/demo/DemoDocumentStatusCard';
import { DemoSettingsPanel } from '../components/demo/DemoSettingsPanel';
import { DemoSidebar } from '../components/demo/DemoSidebar';
import { ErrorBanner } from '../components/errors/ErrorBanner';
import { AppLayout } from '../components/layout/AppLayout';
import { useDemoChat } from '../hooks/useDemoChat';
import { useDemoConversations } from '../hooks/useDemoConversations';

const DOC_ID_KEY = 'enterprise.demo.externalDocumentId';

export function DemoChatPage() {
  const {
    conversations,
    createConversation,
    setConversationTitle,
    resolveConversation,
    refresh: refreshConversations,
  } = useDemoConversations();
  const [externalDocumentId, setExternalDocumentId] = useState(() => {
    const fromEnv = import.meta.env.VITE_DEMO_EXTERNAL_DOCUMENT_ID as
      | string
      | undefined;
    return fromEnv ?? localStorage.getItem(DOC_ID_KEY) ?? '';
  });
  const [tokenConfigured, setTokenConfigured] = useState(() =>
    Boolean(getDemoToken()),
  );
  const [activeId, setActiveId] = useState<string | null>(null);
  const [documentStatus, setDocumentStatus] =
    useState<DemoDocumentStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const [statusError, setStatusError] = useState<ErrorResponse | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [drawerCitation, setDrawerCitation] = useState<Citation | null>(null);
  const hasInteractedRef = useRef(false);

  const onConversationTitle = useCallback(
    (conversationId: string, title: string) => {
      setConversationTitle(conversationId, title);
    },
    [setConversationTitle],
  );

  const onConversationCreated = useCallback(
    (localId: string, conversationId: string) => {
      resolveConversation(localId, conversationId);
      setActiveId(conversationId);
    },
    [resolveConversation],
  );

  const chat = useDemoChat({
    conversationId: activeId,
    externalDocumentId,
    restoreHistory: Boolean(
      conversations.find((item) => item.conversationId === activeId)?.persisted,
    ),
    onConversationTitle,
    onConversationCreated,
  });

  const loadStatus = useCallback((docId: string) => {
    if (!docId) {
      setDocumentStatus(null);
      setStatusError(null);
      return;
    }
    setStatusLoading(true);
    setStatusError(null);
    demoApi
      .getDocumentStatus(docId)
      .then(setDocumentStatus)
      .catch((err) => {
        setDocumentStatus(null);
        setStatusError(normalizeError(err));
      })
      .finally(() => setStatusLoading(false));
  }, []);

  const refreshStatus = useCallback(() => {
    loadStatus(externalDocumentId);
  }, [externalDocumentId, loadStatus]);

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus]);

  useEffect(() => {
    if (!hasInteractedRef.current && conversations.length > 0 && !activeId) {
      setActiveId(conversations[0].conversationId);
    }
  }, [conversations, activeId]);

  const sidebarConversations = useMemo<Conversation[]>(
    () =>
      conversations.map((meta) => ({
        conversationId: meta.conversationId,
        ragflowSessionId: '',
        createdAt: meta.createdAt,
        title: meta.title ?? '新会话',
      })),
    [conversations],
  );

  const activeConversation = useMemo<Conversation | null>(() => {
    const meta = conversations.find((item) => item.conversationId === activeId);
    if (!meta) return null;
    return {
      conversationId: meta.conversationId,
      ragflowSessionId: '',
      createdAt: meta.createdAt,
      title: meta.title ?? '新会话',
    };
  }, [conversations, activeId]);

  const citationMap = useMemo(() => {
    const map = new Map<string, Citation>();
    for (const msg of chat.messages) {
      if (msg.role === 'assistant') {
        for (const cit of msg.citations) {
          map.set(cit.citationId, cit);
        }
      }
    }
    return map;
  }, [chat.messages]);

  const handleSelectConversation = useCallback(
    (conversationId: string) => {
      hasInteractedRef.current = true;
      const meta = conversations.find(
        (item) => item.conversationId === conversationId,
      );
      if (meta) {
        setExternalDocumentId(meta.externalDocumentId);
        localStorage.setItem(DOC_ID_KEY, meta.externalDocumentId);
      }
      setActiveId(conversationId);
      setDrawerCitation(null);
      setSettingsOpen(false);
    },
    [conversations],
  );

  const handleNewConversation = useCallback(() => {
    hasInteractedRef.current = true;
    if (!externalDocumentId) {
      setStatusError({
        code: 'DOCUMENT_NOT_CONFIGURED',
        message: '请先配置 externalDocumentId',
        requestId: 'demo-config',
      });
      setSettingsOpen(true);
      return;
    }
    chat.clearMessages();
    setDrawerCitation(null);
    setActiveId(createConversation(externalDocumentId));
  }, [chat, createConversation, externalDocumentId]);

  const handleSaveSettings = useCallback(
    (docId: string, token: string) => {
      hasInteractedRef.current = true;
      setExternalDocumentId(docId);
      localStorage.setItem(DOC_ID_KEY, docId);
      if (token) {
        setDemoToken(token);
      }
      setTokenConfigured(Boolean(getDemoToken()));
      setSettingsOpen(false);
      setActiveId(null);
      chat.clearMessages();
      setDrawerCitation(null);
      loadStatus(docId);
    },
    [chat, loadStatus],
  );

  const handleCitationClick = useCallback(
    (citationId: string) => {
      const citation = citationMap.get(citationId);
      if (citation) {
        setDrawerCitation(citation);
        setSettingsOpen(false);
      }
    },
    [citationMap],
  );

  const inputDisabled =
    !activeId ||
    !externalDocumentId ||
    !tokenConfigured ||
    documentStatus?.status !== 'ready';

  const drawerContent = settingsOpen ? (
    <DemoSettingsPanel
      externalDocumentId={externalDocumentId}
      tokenConfigured={tokenConfigured}
      status={documentStatus}
      statusLoading={statusLoading}
      statusError={statusError}
      onSave={handleSaveSettings}
      onRefreshStatus={refreshStatus}
      onClose={() => setSettingsOpen(false)}
    />
  ) : drawerCitation ? (
    <CitationDrawer
      citationId={null}
      citation={drawerCitation}
      onClose={() => setDrawerCitation(null)}
    />
  ) : null;

  return (
    <AppLayout
      sidebar={
        <DemoSidebar
          conversations={sidebarConversations}
          activeId={activeId}
          loading={false}
          onSelect={handleSelectConversation}
          onNew={handleNewConversation}
          onRefresh={refreshConversations}
          onOpenSettings={() => {
            setDrawerCitation(null);
            setSettingsOpen(true);
          }}
        />
      }
      main={
        <div className="flex flex-col h-full">
          {(chat.error || statusError) && (
            <ErrorBanner
              error={chat.error ?? statusError!}
              onDismiss={() => {}}
            />
          )}
          <DemoDocumentStatusCard
            externalDocumentId={externalDocumentId}
            status={documentStatus}
            loading={statusLoading}
            error={statusError}
            onRefresh={refreshStatus}
          />
          <div className="flex-1 flex flex-col min-h-0">
            <ChatArea
              activeConversation={activeConversation}
              messages={chat.messages}
              isStreaming={chat.isStreaming}
              inputDisabled={inputDisabled}
              onSend={chat.sendMessage}
              onCancel={chat.cancelStream}
              onCitationClick={handleCitationClick}
            />
          </div>
        </div>
      }
      drawer={drawerContent}
    />
  );
}
