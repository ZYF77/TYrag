import { useCallback, useEffect, useRef, useState } from 'react';
import { toDisplayError, v2Api } from '../api/v2Client';
import type {
  Citation,
  DisplayError,
  HarnessAssistantMessage,
  HarnessMessage,
  HarnessUserMessage,
  Message,
  MessageStatus,
  SseEvent,
} from '../api/v2Types';

interface RetryRequest {
  replyId: string;
  clientMessageId: string;
  question: string;
}

function newClientMessageId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `client-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function readJson(data: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(data);
    return typeof parsed === 'object' && parsed !== null
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function isMessageStatus(value: unknown): value is MessageStatus {
  return value === '已完成' || value === '无可靠依据' || value === '失败';
}

function isFailedStatus(status: string): boolean {
  return status === '失败' || status === 'failed';
}

function isCitation(value: unknown): value is Citation {
  if (typeof value !== 'object' || value === null) return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.citationId === 'string' &&
    typeof item.sourceType === 'string' &&
    typeof item.title === 'string' &&
    ('externalDocumentId' in item || 'sourceVersionId' in item)
  );
}

function addCitation(message: HarnessAssistantMessage, citation: Citation): HarnessAssistantMessage {
  if (message.citations.some((item) => item.citationId === citation.citationId)) {
    return message;
  }
  return { ...message, citations: [...message.citations, citation] };
}

function historyMessage(message: Message, index: number): HarnessMessage {
  const clientMessageId = `history-${message.messageId}`;
  if (message.role === 'user') {
    const user: HarnessUserMessage = {
      id: `${message.messageId}-${index}`,
      role: 'user',
      content: message.content,
      createdAt: message.createdAt,
      clientMessageId,
    };
    return user;
  }
  const assistant: HarnessAssistantMessage = {
    id: `${message.messageId}-${index}`,
    role: 'assistant',
    content: message.content,
    status: message.status,
    citations: message.citations,
    createdAt: message.createdAt,
    clientMessageId,
  };
  return assistant;
}

function eventError(data: Record<string, unknown>): DisplayError {
  const nested = typeof data.error === 'object' && data.error !== null
    ? (data.error as Record<string, unknown>)
    : data;
  return {
    code: typeof nested.code === 'string' ? nested.code : 'RUN_FAILED',
    message: typeof nested.message === 'string' ? nested.message : '回答运行失败',
    requestId: typeof nested.requestId === 'string' ? nested.requestId : 'sse-run-failed',
    retryable: nested.retryable === true,
  };
}

export function useV2Chat(conversationId: string | null) {
  const [messages, setMessages] = useState<HarnessMessage[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<DisplayError | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const retryRef = useRef<RetryRequest | null>(null);

  const updateReply = useCallback(
    (replyId: string, update: (message: HarnessAssistantMessage) => HarnessAssistantMessage) => {
      setMessages((previous) =>
        previous.map((message) =>
          message.role === 'assistant' && message.id === replyId ? update(message) : message,
        ),
      );
    },
    [],
  );

  const startStream = useCallback(
    (request: RetryRequest) => {
      if (!conversationId) return;
      setIsStreaming(true);
      setError(null);

      const handleEvent = (event: SseEvent) => {
        const data = readJson(event.data);
        if (!data) return;

        if (event.event === 'run.started') {
          updateReply(request.replyId, (message) => ({
            ...message,
            runId: typeof data.runId === 'string' ? data.runId : message.runId,
            replayed: data.replayed === true ? true : message.replayed,
          }));
          return;
        }

        if (event.event === 'answer.delta') {
          const content = typeof data.content === 'string' ? data.content : '';
          if (content) {
            updateReply(request.replyId, (message) => ({
              ...message,
              content: message.content + content,
            }));
          }
          return;
        }

        if (event.event === 'citation') {
          if (isCitation(data)) {
            updateReply(request.replyId, (message) => addCitation(message, data));
          } else if (typeof data.citationId === 'string') {
            void v2Api
              .getCitation(data.citationId)
              .then((citation) => {
                updateReply(request.replyId, (message) => addCitation(message, citation));
              })
              .catch((citationError) => {
                const displayError = toDisplayError(citationError);
                updateReply(request.replyId, (message) => ({
                  ...message,
                  citationError: `引用 ${data.citationId} 加载失败：${displayError.message}`,
                }));
              });
          }
          return;
        }

        if (event.event === 'answer.completed') {
          const status = isMessageStatus(data.status) ? data.status : '失败';
          const citations = Array.isArray(data.citations)
            ? data.citations.filter(isCitation)
            : [];
          updateReply(request.replyId, (message) => ({
            ...message,
            status,
            // An explicit empty citation array is meaningful and must not be
            // replaced with evidence from an earlier stream event.
            citations: Array.isArray(data.citations) ? citations : message.citations,
            runId: typeof data.runId === 'string' ? data.runId : message.runId,
          }));
          if (!isFailedStatus(status)) retryRef.current = null;
          return;
        }

        if (event.event === 'run.failed') {
          const displayError = eventError(data);
          setError(displayError);
          retryRef.current = request;
          updateReply(request.replyId, (message) => ({
            ...message,
            status: '失败',
            error: displayError,
            runId: typeof data.runId === 'string' ? data.runId : message.runId,
          }));
        }
      };

      const stream = v2Api.streamMessage(
        conversationId,
        { clientMessageId: request.clientMessageId, question: request.question },
        handleEvent,
      );
      controllerRef.current = stream.controller;
      void stream.promise
        .catch((streamError: unknown) => {
          if (streamError instanceof DOMException && streamError.name === 'AbortError') return;
          const displayError = toDisplayError(streamError);
          setError(displayError);
          retryRef.current = request;
          updateReply(request.replyId, (message) => ({
            ...message,
            status: '失败',
            error: displayError,
          }));
        })
        .finally(() => {
          setIsStreaming(false);
          controllerRef.current = null;
        });
    },
    [conversationId, updateReply],
  );

  useEffect(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    retryRef.current = null;
    setMessages([]);
    setError(null);
    setIsStreaming(false);
    if (!conversationId) return;

    let cancelled = false;
    void v2Api
      .listMessages(conversationId)
      .then((page) => {
        if (!cancelled) setMessages(page.items.map(historyMessage));
      })
      .catch((loadError: unknown) => {
        if (!cancelled) setError(toDisplayError(loadError));
      });

    return () => {
      cancelled = true;
      controllerRef.current?.abort();
    };
  }, [conversationId]);

  const sendMessage = useCallback(
    (question: string) => {
      if (!conversationId || isStreaming) return;
      const clientMessageId = newClientMessageId();
      const replyId = `reply-${clientMessageId}`;
      const now = new Date().toISOString();
      const user: HarnessUserMessage = {
        id: `user-${clientMessageId}`,
        role: 'user',
        content: question,
        createdAt: now,
        clientMessageId,
      };
      const reply: HarnessAssistantMessage = {
        id: replyId,
        role: 'assistant',
        content: '',
        status: 'streaming',
        citations: [],
        createdAt: now,
        clientMessageId,
      };
      setMessages((previous) => [...previous, user, reply]);
      startStream({ replyId, clientMessageId, question });
    },
    [conversationId, isStreaming, startStream],
  );

  const retry = useCallback(() => {
    if (!isStreaming && retryRef.current) {
      const request = retryRef.current;
      updateReply(request.replyId, (message) => ({
        ...message,
        content: '',
        citations: [],
        citationError: undefined,
        error: undefined,
        status: 'streaming',
      }));
      startStream(request);
    }
  }, [isStreaming, startStream, updateReply]);

  const cancelStream = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setIsStreaming(false);
  }, []);

  const clearMessages = useCallback(() => {
    setMessages([]);
    setError(null);
    retryRef.current = null;
  }, []);

  return {
    messages,
    isStreaming,
    error,
    sendMessage,
    retry,
    cancelStream,
    clearMessages,
  };
}
