import { createSseParser } from './sse';
import type {
  ConversationDetail,
  ConversationPage,
  CreateConversationRequest,
  CreateMessageRequest,
  Citation,
  DisplayError,
  DocumentCommand,
  DocumentOperation,
  DocumentOperationPage,
  ErrorResponse,
  MessagePage,
  MessageRunResult,
  MessageRunPending,
  PatchConversationContextRequest,
  SseEvent,
  SuggestionPage,
} from './v2Types';

const BASE = '/enterprise/api/v2';
const TOKEN_STORAGE_KEY = 'enterprise.harness.jwt';

export const HARNESS_DEFAULTS = {
  tenantId: (import.meta.env.VITE_HARNESS_TENANT_ID as string | undefined) ?? 'demo-tenant',
  sourceSystem:
    (import.meta.env.VITE_HARNESS_SOURCE_SYSTEM as string | undefined) ?? 'equipment-system',
};

export class V2ApiError extends Error {
  constructor(
    public status: number,
    public body: ErrorResponse,
  ) {
    super(body.message);
    this.name = 'V2ApiError';
  }
}

export function getHarnessToken(): string {
  const stored = sessionStorage.getItem(TOKEN_STORAGE_KEY);
  if (stored) return stored;
  return (import.meta.env.VITE_DEMO_JWT as string | undefined) ?? '';
}

export function setHarnessToken(token: string): void {
  const value = token.trim();
  if (value) {
    sessionStorage.setItem(TOKEN_STORAGE_KEY, value);
  } else {
    sessionStorage.removeItem(TOKEN_STORAGE_KEY);
  }
}

function authHeaders(): Record<string, string> {
  const token = getHarnessToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function normalizeErrorBody(value: unknown, status: number): ErrorResponse {
  const raw = isRecord(value) && isRecord(value.detail) ? value.detail : value;
  return {
    code: isRecord(raw) && typeof raw.code === 'string' ? raw.code : `HTTP_${status}`,
    message:
      isRecord(raw) && typeof raw.message === 'string'
        ? raw.message
        : `Gateway returned HTTP ${status}`,
    requestId:
      isRecord(raw) && typeof raw.requestId === 'string' ? raw.requestId : 'unknown',
    retryable: isRecord(raw) && raw.retryable === true,
    ...(isRecord(raw) && isRecord(raw.details) ? { details: raw.details } : {}),
  };
}

async function errorFromResponse(response: Response): Promise<V2ApiError> {
  const payload = await response.json().catch(() => undefined);
  return new V2ApiError(response.status, normalizeErrorBody(payload, response.status));
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...authHeaders(),
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
        ...(init.headers ?? {}),
      },
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new V2ApiError(0, {
      code: 'GATEWAY_UNAVAILABLE',
      message: 'Gateway 不可用或网络异常',
      requestId: 'gateway-unavailable',
      retryable: true,
    });
  }

  if (!response.ok) throw await errorFromResponse(response);
  return (await response.json()) as T;
}

function queryPath(
  path: string,
  values: Record<string, string | number | null | undefined>,
): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== '') {
      query.set(key, String(value));
    }
  }
  const encoded = query.toString();
  return encoded ? `${path}?${encoded}` : path;
}

export interface StreamHandle {
  controller: AbortController;
  promise: Promise<void>;
}

const pendingDelay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

function compatibleSignal(controller: AbortController): AbortSignal | undefined {
  try {
    if (typeof Request === 'function') {
      new Request(BASE, { signal: controller.signal });
    }
    return controller.signal;
  } catch {
    // jsdom's AbortSignal and Node fetch's AbortSignal are different realms.
    // Browser production requests still use the native signal.
    return undefined;
  }
}

function emitJsonResult(result: MessageRunResult, onEvent: (event: SseEvent) => void): void {
  onEvent({
    event: 'run.started',
    data: JSON.stringify({
      conversationId: result.conversationId,
      clientMessageId: result.clientMessageId,
      runId: result.runId,
      replayed: result.replayed,
    }),
  });
  if (result.answer) {
    onEvent({ event: 'answer.delta', data: JSON.stringify({ content: result.answer }) });
  }
  for (const citation of result.citations) {
    onEvent({ event: 'citation', data: JSON.stringify(citation) });
  }
  onEvent({
    event: 'answer.completed',
    data: JSON.stringify({
      conversationId: result.conversationId,
      runId: result.runId,
      messageId: result.messageId,
      status: result.status,
      citations: result.citations,
    }),
  });
}

export function streamMessage(
  conversationId: string,
  body: CreateMessageRequest,
  onEvent: (event: SseEvent) => void,
): StreamHandle {
  const controller = new AbortController();
  const signal = compatibleSignal(controller);
  const promise = (async () => {
    const url = `${BASE}/conversations/${encodeURIComponent(conversationId)}/messages`;
    const init: RequestInit = {
      method: 'POST',
      headers: {
        Accept: 'text/event-stream',
        'Content-Type': 'application/json',
        ...authHeaders(),
      },
      body: JSON.stringify(body),
      ...(signal ? { signal } : {}),
    };
    let response: Response;
    for (let attempt = 0; ; attempt += 1) {
      try {
        response = await fetch(url, init);
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') throw error;
        throw new V2ApiError(0, {
          code: 'GATEWAY_UNAVAILABLE',
          message: 'Gateway 不可用或网络异常',
          requestId: 'gateway-unavailable',
          retryable: true,
        });
      }
      if (!response.ok) throw await errorFromResponse(response);
      if (response.status !== 202) break;
      const pending = (await response.json()) as MessageRunPending;
      onEvent({ event: 'run.started', data: JSON.stringify(pending) });
      if (attempt >= 39) {
        throw new V2ApiError(202, {
          code: 'RUN_PENDING_TIMEOUT',
          message: '回答运行仍在处理中，请稍后重试。',
          requestId: pending.runId,
          retryable: true,
        });
      }
      await pendingDelay(50);
    }

    const contentType = response.headers.get('content-type') ?? '';
    if (!contentType.includes('text/event-stream')) {
      const result = (await response.json()) as MessageRunResult | MessageRunPending;
      if ('answer' in result) {
        emitJsonResult(result, onEvent);
      } else {
        onEvent({ event: 'run.started', data: JSON.stringify(result) });
      }
      return;
    }

    if (!response.body) {
      throw new V2ApiError(0, {
        code: 'SSE_BODY_MISSING',
        message: 'SSE 响应体为空',
        requestId: 'sse-body-missing',
        retryable: true,
      });
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const parser = createSseParser(onEvent);
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
    parser.feed(decoder.decode());
    parser.end();
  })();

  return { controller, promise };
}

export const v2Api = {
  submitDocument(command: DocumentCommand): Promise<DocumentOperation> {
    return request<DocumentOperation>('/documents', {
      method: 'POST',
      body: JSON.stringify(command),
    });
  },

  getDocumentStatus(
    externalDocumentId: string,
    params: { tenantId?: string; sourceSystem?: string; sourceVersionId?: string } = {},
  ): Promise<DocumentOperation> {
    return request<DocumentOperation>(
      queryPath(`/documents/${encodeURIComponent(externalDocumentId)}/status`, {
        tenantId: params.tenantId ?? HARNESS_DEFAULTS.tenantId,
        sourceSystem: params.sourceSystem ?? HARNESS_DEFAULTS.sourceSystem,
        sourceVersionId: params.sourceVersionId,
      }),
    );
  },

  listDocumentStatus(
    params: { tenantId?: string; sourceSystem?: string; limit?: number; cursor?: string } = {},
  ): Promise<DocumentOperationPage> {
    return request<DocumentOperationPage>(
      queryPath('/documents/sync-status', {
        tenantId: params.tenantId ?? HARNESS_DEFAULTS.tenantId,
        sourceSystem: params.sourceSystem ?? HARNESS_DEFAULTS.sourceSystem,
        limit: params.limit,
        cursor: params.cursor,
      }),
    );
  },

  createConversation(body: CreateConversationRequest = {}): Promise<ConversationDetail> {
    return request<ConversationDetail>('/conversations', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  listConversations(params: { limit?: number; cursor?: string } = {}): Promise<ConversationPage> {
    return request<ConversationPage>(queryPath('/conversations', params));
  },

  getConversation(conversationId: string): Promise<ConversationDetail> {
    return request<ConversationDetail>(
      `/conversations/${encodeURIComponent(conversationId)}`,
    );
  },

  patchConversationContext(
    conversationId: string,
    body: PatchConversationContextRequest,
  ): Promise<ConversationDetail> {
    return request<ConversationDetail>(
      `/conversations/${encodeURIComponent(conversationId)}/context`,
      { method: 'PATCH', body: JSON.stringify(body) },
    );
  },

  listMessages(conversationId: string, params: { limit?: number; cursor?: string } = {}): Promise<MessagePage> {
    return request<MessagePage>(
      queryPath(`/conversations/${encodeURIComponent(conversationId)}/messages`, params),
    );
  },

  listSuggestions(conversationId: string): Promise<SuggestionPage> {
    return request<SuggestionPage>(
      `/conversations/${encodeURIComponent(conversationId)}/suggestions`,
    );
  },

  getCitation(citationId: string): Promise<Citation> {
    return request<Citation>(`/citations/${encodeURIComponent(citationId)}`);
  },

  streamMessage,
};

export function toDisplayError(error: unknown): DisplayError {
  if (error instanceof V2ApiError) {
    return { ...error.body, httpStatus: error.status };
  }
  if (error instanceof DOMException && error.name === 'AbortError') {
    return {
      code: 'REQUEST_CANCELLED',
      message: '请求已取消',
      requestId: 'request-cancelled',
      retryable: false,
    };
  }
  return {
    code: 'GATEWAY_UNAVAILABLE',
    message: 'Gateway 不可用或网络异常',
    requestId: 'gateway-unavailable',
    retryable: true,
    httpStatus: 0,
  };
}
