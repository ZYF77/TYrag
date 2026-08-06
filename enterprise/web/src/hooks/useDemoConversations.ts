import { useCallback, useState } from 'react';

const STORAGE_KEY = 'enterprise.demo.conversations';

export interface DemoConversationMeta {
  conversationId: string;
  externalDocumentId: string;
  createdAt: string;
  title?: string;
  persisted?: boolean;
}

function loadConversations(): DemoConversationMeta[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as DemoConversationMeta[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveConversations(list: DemoConversationMeta[]): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
}

function newConversationId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `demo-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export function useDemoConversations() {
  const [conversations, setConversations] = useState<DemoConversationMeta[]>(
    loadConversations,
  );

  const refresh = useCallback(() => {
    setConversations(loadConversations());
  }, []);

  const createConversation = useCallback((externalDocumentId: string) => {
    const conversationId = newConversationId();
    const meta: DemoConversationMeta = {
      conversationId,
      externalDocumentId,
      createdAt: new Date().toISOString(),
      persisted: false,
    };
    setConversations((prev) => {
      const next = [meta, ...prev];
      saveConversations(next);
      return next;
    });
    return conversationId;
  }, []);

  const setConversationTitle = useCallback(
    (conversationId: string, title: string) => {
      setConversations((prev) => {
        const next = prev.map((item) =>
          item.conversationId === conversationId
            ? { ...item, title, persisted: true }
            : item,
        );
        saveConversations(next);
        return next;
      });
    },
    [],
  );

  const resolveConversation = useCallback(
    (localId: string, conversationId: string) => {
      setConversations((prev) => {
        const next = prev.map((item) =>
          item.conversationId === localId
            ? { ...item, conversationId, persisted: true }
            : item,
        );
        saveConversations(next);
        return next;
      });
    },
    [],
  );

  return {
    conversations,
    createConversation,
    setConversationTitle,
    resolveConversation,
    refresh,
  };
}
