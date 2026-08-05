import { useState, useCallback, useMemo } from 'react';
import { AppLayout } from '../components/layout/AppLayout';
import { Sidebar } from '../components/layout/Sidebar';
import { ChatArea } from '../components/chat/ChatArea';
import { CitationDrawer } from '../components/citations/CitationDrawer';
import { FileSyncStatus } from '../components/sync/FileSyncStatus';
import { useConversations } from '../hooks/useConversations';
import { useChat } from '../hooks/useChat';
import { useFileSync } from '../hooks/useFileSync';
import { ErrorBanner } from '../components/errors/ErrorBanner';

export function ChatPage() {
  const {
    conversations,
    activeId,
    setActiveId,
    loading: convLoading,
    error: convError,
    createConversation,
    refresh: refreshConversations,
  } = useConversations();

  const {
    messages,
    isStreaming,
    error,
    sendMessage,
    cancelStream,
    clearMessages,
  } = useChat(activeId);

  const {
    items: syncItems,
    loading: syncLoading,
    refresh: refreshSync,
  } = useFileSync();

  const [citationDrawerId, setCitationDrawerId] = useState<string | null>(null);
  const [syncDrawerOpen, setSyncDrawerOpen] = useState(false);

  const activeConversation = useMemo(
    () => conversations.find((c) => c.conversationId === activeId) ?? null,
    [conversations, activeId],
  );

  const handleSelectConversation = useCallback(
    (id: string) => {
      clearMessages();
      setActiveId(id);
    },
    [setActiveId, clearMessages],
  );

  const handleNewConversation = useCallback(async () => {
    clearMessages();
    try {
      await createConversation();
    } catch {
      // Error is already set in useConversations hook state
    }
  }, [createConversation, clearMessages]);

  const drawerContent = useMemo(() => {
    if (citationDrawerId) {
      return (
        <CitationDrawer
          citationId={citationDrawerId}
          onClose={() => setCitationDrawerId(null)}
        />
      );
    }
    if (syncDrawerOpen) {
      return (
        <FileSyncStatus
          items={syncItems}
          loading={syncLoading}
          onClose={() => setSyncDrawerOpen(false)}
          onRefresh={refreshSync}
        />
      );
    }
    return null;
  }, [citationDrawerId, syncDrawerOpen, syncItems, syncLoading, refreshSync]);

  const handleToggleSyncDrawer = useCallback(() => {
    setCitationDrawerId(null);
    setSyncDrawerOpen((prev) => !prev);
  }, []);

  const handleCitationClick = useCallback((citationId: string) => {
    setSyncDrawerOpen(false);
    setCitationDrawerId(citationId);
  }, []);

  return (
    <AppLayout
      sidebar={
        <Sidebar
          conversations={conversations}
          activeId={activeId}
          loading={convLoading}
          onSelect={handleSelectConversation}
          onNew={handleNewConversation}
          onRefresh={refreshConversations}
          syncCount={syncItems.length}
          onToggleSyncDrawer={handleToggleSyncDrawer}
          syncDrawerOpen={syncDrawerOpen}
        />
      }
      main={
        <div className="flex flex-col h-full">
          {(error || convError) && (
            <ErrorBanner
              error={error || convError!}
              onDismiss={() => {}}
            />
          )}
          <div className="flex-1 flex flex-col min-h-0">
            <ChatArea
              activeConversation={activeConversation}
              messages={messages}
              isStreaming={isStreaming}
              onSend={sendMessage}
              onCancel={cancelStream}
              onCitationClick={handleCitationClick}
            />
          </div>
        </div>
      }
      drawer={drawerContent}
    />
  );
}
