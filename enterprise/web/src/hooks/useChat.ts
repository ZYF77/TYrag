import { useState, useCallback, useRef } from 'react';
import { api } from '../api/client';
import type {
  ReplyMessage,
  UserMessage,
  Citation,
  ErrorResponse,
  SseEvent,
} from '../api/types';

export function useChat(conversationId: string | null) {
  const [messages, setMessages] = useState<(UserMessage | ReplyMessage)[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<ErrorResponse | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const sendMessage = useCallback(
    (question: string) => {
      if (!conversationId || isStreaming) return;

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

      const pendingCitations: string[] = [];

      const controller = api.streamAsk(
        conversationId,
        { question },
        (event: SseEvent) => {
          setMessages((prev) => {
            const updated = [...prev];
            const lastIdx = updated.length - 1;
            if (lastIdx < 0) return prev;
            const last = { ...updated[lastIdx] } as ReplyMessage;

            switch (event.event) {
              case 'answer.delta': {
                const data = JSON.parse(event.data) as { content: string };
                last.content += data.content;
                break;
              }
              case 'citation': {
                const data = JSON.parse(event.data) as { citationId: string };
                pendingCitations.push(data.citationId);
                break;
              }
              case 'answer.completed': {
                const data = JSON.parse(event.data) as {
                  runId: string;
                  status?: string;
                };
                if (data.status === 'no_evidence') {
                  last.status = 'no_evidence';
                  last.content = '未找到可靠的证据来回答此问题。请尝试调整您的问题，或联系知识管理员补充相关文档。';
                } else {
                  last.status = 'completed';
                }
                break;
              }
              case 'run.failed': {
                const data = JSON.parse(event.data) as {
                  error: ErrorResponse;
                };
                last.status = 'failed';
                last.error = data.error;
                break;
              }
            }

            updated[lastIdx] = last;
            return updated;
          });
        },
        (err: ErrorResponse | Error) => {
          setError(
            'code' in err ? err : { code: 'UNKNOWN', message: err.message, requestId: 'unknown' },
          );
          setMessages((prev) => {
            const updated = [...prev];
            const lastIdx = updated.length - 1;
            if (lastIdx >= 0) {
              const last = { ...updated[lastIdx] } as ReplyMessage;
              last.status = 'failed';
              last.error = 'code' in err ? err : undefined;
              updated[lastIdx] = last;
            }
            return updated;
          });
        },
        async () => {
          // Stream ended: resolve pending citations
          setIsStreaming(false);

          if (pendingCitations.length > 0) {
            const resolvedCitations: Citation[] = [];
            const failedCitationIds: string[] = [];
            for (const cid of pendingCitations) {
              try {
                const cit = await api.getCitation(cid);
                resolvedCitations.push(cit);
              } catch {
                failedCitationIds.push(cid);
              }
            }

            setMessages((prev) => {
              const updated = [...prev];
              const lastIdx = updated.length - 1;
              if (lastIdx >= 0) {
                const last = { ...updated[lastIdx] } as ReplyMessage;
                last.citations = resolvedCitations;
                if (failedCitationIds.length > 0 && last.status === 'completed') {
                  last.status = 'degraded';
                  if (!last.error) {
                    last.error = {
                      code: 'CITATION_PARTIAL',
                      message: failedCitationIds.length + ' 个引用未能加载',
                      requestId: 'cit-fail',
                    };
                  }
                }
                updated[lastIdx] = last;
              }
              return updated;
            });
          }
        },
      );

      abortRef.current = controller;
    },
    [conversationId, isStreaming],
  );

  const cancelStream = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setIsStreaming(false);
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