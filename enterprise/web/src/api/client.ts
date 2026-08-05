import type {
  Conversation,
  CreateConversationRequest,
  AskRequest,
  Citation,
  FileSyncItem,
  DocumentSyncResponse,
  ErrorResponse,
  SseEvent,
} from './types';

const BASE = '/enterprise/api/v1';

class ApiError extends Error {
  constructor(
    public status: number,
    public body: ErrorResponse,
  ) {
    super(body.message);
    this.name = 'ApiError';
  }
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = (await res.json().catch(() => ({
      code: 'UNKNOWN',
      message: `HTTP ${res.status}`,
      requestId: 'unknown',
    }))) as ErrorResponse;
    throw new ApiError(res.status, body);
  }
  return res.json();
}

export const api = {
  // ---- Conversations ----

  async createConversation(
    req: CreateConversationRequest = {},
  ): Promise<Conversation> {
    const res = await fetch(`${BASE}/conversations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    });
    return handleResponse<Conversation>(res);
  },

  async listConversations(): Promise<Conversation[]> {
    const res = await fetch(`${BASE}/conversations`);
    return handleResponse<Conversation[]>(res);
  },

  // ---- Chat (SSE) ----

  streamAsk(
    conversationId: string,
    req: AskRequest,
    onEvent: (event: SseEvent) => void,
    onError: (error: ErrorResponse | Error) => void,
    onComplete: () => void,
  ): AbortController {
    const controller = new AbortController();

    (async () => {
      try {
        const response = await fetch(
          `${BASE}/conversations/${conversationId}/messages:stream`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req),
            signal: controller.signal,
          },
        );

        if (!response.ok) {
          try {
            const body = (await response.json()) as ErrorResponse;
            onError(body);
          } catch {
            onError({
              code: `HTTP_${response.status}`,
              message: `请求失败 (HTTP ${response.status})`,
              requestId: 'stream-err',
            });
          }
          onComplete();
          return;
        }

        if (!response.body) {
          onError({ code: 'NO_BODY', message: '响应体为空', requestId: 'no-body' });
          onComplete();
          return;
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let currentEvent = '';
        let currentData = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });

          // Process complete lines; keep partial last line in buffer
          let newlineIdx: number;
          while ((newlineIdx = buffer.indexOf('\n')) !== -1) {
            const line = buffer.slice(0, newlineIdx).replace(/\r$/, '');
            buffer = buffer.slice(newlineIdx + 1);

            if (line.startsWith('event: ')) {
              currentEvent = line.slice(7).trim();
            } else if (line.startsWith('data: ')) {
              // Concatenate multi-line data
              currentData = currentData
                ? currentData + '\n' + line.slice(6).trim()
                : line.slice(6).trim();
            } else if (line === '') {
              // Empty line: dispatch complete event
              if (currentEvent) {
                onEvent({ event: currentEvent as SseEvent['event'], data: currentData });
                currentEvent = '';
                currentData = '';
              }
            }
            // Ignore comment lines (starting with ':')
          }
        }

        // Dispatch any remaining event at end of stream
        if (currentEvent) {
          onEvent({ event: currentEvent as SseEvent['event'], data: currentData });
        }
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') {
          // Intentionally aborted - no error
        } else {
          onError({
            code: 'NETWORK_ERROR',
            message: err instanceof Error ? err.message : String(err),
            requestId: 'net-err',
          });
        }
      } finally {
        onComplete();
      }
    })();

    return controller;
  },

  // ---- Citations ----

  async getCitation(citationId: string): Promise<Citation> {
    const res = await fetch(`${BASE}/citations/${citationId}`);
    return handleResponse<Citation>(res);
  },

  // ---- File Sync ----

  async getDocumentStatus(
    externalDocumentId: string,
  ): Promise<DocumentSyncResponse> {
    const res = await fetch(
      `${BASE}/documents/${externalDocumentId}/status`,
    );
    return handleResponse<DocumentSyncResponse>(res);
  },

  async listSyncStatus(): Promise<FileSyncItem[]> {
    const res = await fetch(`${BASE}/documents/sync-status`);
    return handleResponse<FileSyncItem[]>(res);
  },
};
