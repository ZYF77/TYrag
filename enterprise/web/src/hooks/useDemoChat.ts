import { useCallback, useEffect, useState } from 'react';
import { demoApi } from '../api/demoClient';
import { normalizeError } from '../api/errors';
import type {
  ChatMessage,
  ErrorResponse,
  ReplyMessage,
  UserMessage,
} from '../api/types';

interface UseDemoChatOptions {
  conversationId: string | null;
  externalDocumentId: string;
  restoreHistory?: boolean;
  onConversationTitle?: (conversationId: string, title: string) => void;
  onConversationCreated?: (localId: string, conversationId: string) => void;
}

export function useDemoChat({
  conversationId,
  externalDocumentId,
  restoreHistory,
  onConversationTitle,
  onConversationCreated,
}: UseDemoChatOptions) {
  const [messages, setMessages] = useState<(UserMessage | ReplyMessage)[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<ErrorResponse | null>(null);

  useEffect(() => {
    if (!conversationId || !restoreHistory) {
      setMessages([]);
      setError(null);
      return;
    }

    let cancelled = false;
    setError(null);
    demoApi
      .getConversation(conversationId)
      .then((data) => {
        if (cancelled) return;
        const restored = data.messages.map<ChatMessage>((msg, index) => {
          const base = {
            id: msg.messageId ? `${msg.messageId}-${index}` : `restored-${index}`,
            createdAt: msg.createdAt || new Date().toISOString(),
          };
          if (msg.role === 'user') {
            return { ...base, role: 'user', content: msg.content };
          }
          const status: ReplyMessage['status'] =
            msg.status === 'failed' || msg.status === 'no_reliable_evidence'
              ? msg.status
              : 'completed';
          return {
            ...base,
            role: 'assistant',
            content: msg.content,
            citations: msg.citations,
            status,
          };
        });
        setMessages(restored);
        const firstUser = restored.find((msg) => msg.role === 'user');
        if (firstUser?.content) {
          onConversationTitle?.(conversationId, firstUser.content.slice(0, 30));
        }
      })
      .catch((err) => {
        if (!cancelled) setError(normalizeError(err));
      });

    return () => {
      cancelled = true;
    };
  }, [conversationId, restoreHistory, onConversationTitle]);

  const sendMessage = useCallback(
    (question: string) => {
      if (!conversationId || isStreaming) return;
      if (!externalDocumentId) {
        setError({
          code: 'DOCUMENT_NOT_CONFIGURED',
          message: '请先配置 externalDocumentId',
          requestId: 'demo-config',
        });
        return;
      }

      const userMsg: UserMessage = {
        id: `user-${Date.now()}`,
        role: 'user',
        content: question,
        createdAt: new Date().toISOString(),
      };
      const replyMsg: ReplyMessage = {
        id: `reply-${Date.now()}`,
        role: 'assistant',
        content: '',
        citations: [],
        status: 'streaming',
        createdAt: new Date().toISOString(),
      };

      setMessages((prev) => [...prev, userMsg, replyMsg]);
      setIsStreaming(true);
      setError(null);

      demoApi
        .ask({
          externalDocumentId,
          question,
          conversationId: restoreHistory ? conversationId : undefined,
        })
        .then((data) => {
          setMessages((prev) => {
            const updated = [...prev];
            const lastIdx = updated.length - 1;
            if (lastIdx < 0) return prev;
            const last = { ...updated[lastIdx] } as ReplyMessage;
            last.content = data.answer;
            last.citations = data.citations;
            last.status = data.status;
            updated[lastIdx] = last;
            return updated;
          });
          if (restoreHistory) {
            onConversationTitle?.(conversationId, question.slice(0, 30));
          } else if (data.conversationId !== conversationId) {
            onConversationCreated?.(conversationId, data.conversationId);
          } else {
            onConversationTitle?.(conversationId, question.slice(0, 30));
          }
        })
        .catch((err) => {
          const normalized = normalizeError(err);
          setError(normalized);
          setMessages((prev) => {
            const updated = [...prev];
            const lastIdx = updated.length - 1;
            if (lastIdx < 0) return prev;
            const last = { ...updated[lastIdx] } as ReplyMessage;
            last.status = 'failed';
            last.error = normalized;
            updated[lastIdx] = last;
            return updated;
          });
        })
        .finally(() => {
          setIsStreaming(false);
        });
    },
    [
      conversationId,
      externalDocumentId,
      isStreaming,
      restoreHistory,
      onConversationTitle,
      onConversationCreated,
    ],
  );

  const cancelStream = useCallback(() => {
    setIsStreaming(false);
    setMessages((prev) => {
      const updated = [...prev];
      const lastIdx = updated.length - 1;
      if (lastIdx >= 0) {
        const last = { ...updated[lastIdx] } as ReplyMessage;
        if (last.status === 'streaming') {
          last.status = 'failed';
        }
        updated[lastIdx] = last;
      }
      return updated;
    });
  }, []);

  const clearMessages = useCallback(() => {
    setMessages([]);
    setError(null);
  }, []);

  return {
    messages,
    isStreaming,
    error,
    sendMessage,
    cancelStream,
    clearMessages,
  };
}
