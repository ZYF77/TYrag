import { createSseParser } from './sse';
import { API_MODE } from './mode';
import { browserDocumentSyncEnabled } from './documentSyncPolicy';
import type { UserPrincipal } from './types';
import type {
  ConversationDetail,
  ConversationAttachmentRequest,
  ConversationAttachmentResponse,
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
import type {
  AdminConversationMessagesPage,
  CallbackBinding,
  ConsoleUserPrincipal,
  ConversationMetadataOrderBy,
  ConversationMetadataPage,
  DocumentMetadataOrderBy,
  DocumentMetadataPage,
  EamProbeResult,
  GatewayHealth,
  GatewayHttpLogPage,
  MetadataSortOrder,
  MetadataSummary,
  SystemIntegrations,
} from './consoleTypes';

const V1_BASE = '/enterprise/api/v1';
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

function requireBrowserDocumentSync(): void {
  if (browserDocumentSyncEnabled(API_MODE)) return;
  throw new V2ApiError(0, {
    code: 'DOCUMENT_PRODUCER_REQUIRED',
    message: '文档同步必须由服务侧 HMAC producer 执行；浏览器不会调用文档接口。',
    requestId: 'browser-document-sync-disabled',
    retryable: false,
  });
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

async function request<T>(
  path: string,
  init: RequestInit = {},
  base = BASE,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
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
  if (response.status === 204) return undefined as T;
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

// Mirrors enterprise/gateway/query/attachment_context.py:
// MESSAGE_MEDIA_TYPES + MAX_MESSAGE_FILES, with the documented 10MB per-file cap.
export const MESSAGE_FILE_LIMITS = {
  maxFiles: 5,
  maxFileBytes: 10 * 1024 * 1024,
  allowedMediaTypes: [
    'image/jpeg',
    'image/png',
    'text/plain',
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  ] as readonly string[],
} as const;

function invalidMessageFilesError(message: string): V2ApiError {
  return new V2ApiError(0, {
    code: 'MESSAGE_FILES_INVALID',
    message,
    requestId: 'message-files-invalid',
    retryable: false,
  });
}

export function validateMessageFiles(files: File[]): void {
  if (files.length > MESSAGE_FILE_LIMITS.maxFiles) {
    throw invalidMessageFilesError(`附件数量超出限制：最多 ${MESSAGE_FILE_LIMITS.maxFiles} 个文件。`);
  }
  for (const file of files) {
    if (file.size === 0) {
      throw invalidMessageFilesError(`附件 ${file.name} 内容为空，无法发送。`);
    }
    if (file.size > MESSAGE_FILE_LIMITS.maxFileBytes) {
      throw invalidMessageFilesError(`附件 ${file.name} 超过单个文件 10MB 限制。`);
    }
    const mediaType = (file.type || '').split(';')[0].trim().toLowerCase();
    if (!MESSAGE_FILE_LIMITS.allowedMediaTypes.includes(mediaType)) {
      throw invalidMessageFilesError(`附件 ${file.name} 的文件类型不受支持，仅支持 JPEG/PNG/TXT/PDF/DOCX/XLSX。`);
    }
  }
}

function multipartFilename(name: string): string {
  // Browsers send the raw UTF-8 name inside filename="…"; only strip characters
  // that would break the header framing (quotes / CR / LF).
  return (name || 'attachment').replace(/[\r\n"]/g, '_');
}

async function readFileBytes(file: File): Promise<Uint8Array> {
  if (typeof file.arrayBuffer === 'function') {
    return new Uint8Array(await file.arrayBuffer());
  }
  // jsdom's Blob predates Blob.arrayBuffer(); FileReader covers that environment.
  const buffer = await new Promise<ArrayBuffer>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error ?? new Error('File content could not be read'));
    reader.readAsArrayBuffer(file);
  });
  return new Uint8Array(buffer);
}

interface MultipartMessage {
  body: Uint8Array;
  contentType: string;
}

/**
 * Serializes { metadata: JSON, files: [...] } into the exact multipart/form-data
 * wire format a browser FormData would produce (same field names, same per-part
 * Content-Type), so Gateway's `_parse_multipart_message` sees identical input.
 * The bytes are sent as a Uint8Array body with an explicit boundary header:
 * native FormData would also work in browsers, but jsdom's File/Blob cannot be
 * streamed by undici under vitest (fetch(…, formData) hangs / body is lost).
 */
async function buildMessageMultipart(
  body: CreateMessageRequest,
  files: File[],
): Promise<MultipartMessage> {
  const boundary = `----harness-message-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  const encoder = new TextEncoder();
  const chunks: Uint8Array[] = [];
  const pushText = (text: string) => chunks.push(encoder.encode(text));

  pushText(`--${boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n${JSON.stringify(body)}\r\n`);
  for (const file of files) {
    const mediaType = file.type || 'application/octet-stream';
    pushText(`--${boundary}\r\nContent-Disposition: form-data; name="files"; filename="${multipartFilename(file.name)}"\r\nContent-Type: ${mediaType}\r\n\r\n`);
    chunks.push(await readFileBytes(file));
    pushText('\r\n');
  }
  pushText(`--${boundary}--\r\n`);

  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  return { body: bytes, contentType: `multipart/form-data; boundary=${boundary}` };
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
  files: File[] = [],
): StreamHandle {
  const controller = new AbortController();
  const signal = compatibleSignal(controller);
  const promise = (async () => {
    const url = `${BASE}/conversations/${encodeURIComponent(conversationId)}/messages`;
    const headers: Record<string, string> = {
      Accept: 'text/event-stream',
      ...authHeaders(),
    };
    let payload: RequestInit['body'];
    if (files.length > 0) {
      // Rejected before any request is sent; errors surface through the
      // stream promise and end up in the chat error banner.
      validateMessageFiles(files);
      const multipart = await buildMessageMultipart(body, files);
      headers['Content-Type'] = multipart.contentType;
      // TS 5.7+ 将 Uint8Array 泛型化后不再直接满足 BodyInit；运行时 fetch 接受二进制 body。
      payload = multipart.body as unknown as RequestInit['body'];
    } else {
      headers['Content-Type'] = 'application/json';
      payload = JSON.stringify(body);
    }
    const init: RequestInit = {
      method: 'POST',
      headers,
      body: payload,
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
  getHealth(): Promise<GatewayHealth> {
    return request<GatewayHealth>('/health', {}, V1_BASE);
  },

  listHttpLog(limit = 100): Promise<GatewayHttpLogPage> {
    return request<GatewayHttpLogPage>(`/diagnostics/http-log?limit=${limit}`, {}, V1_BASE);
  },

  async getAuthMe(): Promise<ConsoleUserPrincipal> {
    const principal = await request<UserPrincipal>('/auth/me', {}, V1_BASE);
    return {
      displayName: principal.displayName,
      tenantId: principal.tenantId,
      roles: principal.roles,
      capabilities: principal.capabilities,
      mappingStatus: principal.mappingStatus,
    };
  },

  // ---- Admin system settings (v1) ----

  getSystemIntegrations(): Promise<SystemIntegrations> {
    return request<SystemIntegrations>('/admin/system/integrations', {}, V1_BASE);
  },

  probeEamCallback(binding: CallbackBinding): Promise<EamProbeResult> {
    return request<EamProbeResult>('/admin/system/eam-probe', {
      method: 'POST',
      body: JSON.stringify({ binding }),
    }, V1_BASE);
  },

  listAdminConversationMetadata(
    params: {
      limit?: number;
      offset?: number;
      status?: string | null;
      orderBy?: ConversationMetadataOrderBy | null;
      order?: MetadataSortOrder | null;
    } = {},
  ): Promise<ConversationMetadataPage> {
    return request<ConversationMetadataPage>(
      queryPath('/admin/system/metadata/conversations', {
        limit: params.limit,
        offset: params.offset,
        status: params.status,
        orderBy: params.orderBy,
        order: params.order,
      }),
      {},
      V1_BASE,
    );
  },

  listAdminDocumentMetadata(
    params: {
      limit?: number;
      offset?: number;
      sourceSystem?: string | null;
      status?: string | null;
      businessStatus?: string | null;
      orderBy?: DocumentMetadataOrderBy | null;
      order?: MetadataSortOrder | null;
    } = {},
  ): Promise<DocumentMetadataPage> {
    return request<DocumentMetadataPage>(
      queryPath('/admin/system/metadata/documents', {
        limit: params.limit,
        offset: params.offset,
        sourceSystem: params.sourceSystem,
        status: params.status,
        businessStatus: params.businessStatus,
        orderBy: params.orderBy,
        order: params.order,
      }),
      {},
      V1_BASE,
    );
  },

  getMetadataSummary(): Promise<MetadataSummary> {
    return request<MetadataSummary>('/admin/system/metadata/summary', {}, V1_BASE);
  },

  getAdminConversationMessages(conversationId: string): Promise<AdminConversationMessagesPage> {
    return request<AdminConversationMessagesPage>(
      `/admin/system/metadata/conversations/${encodeURIComponent(conversationId)}/messages`,
      {},
      V1_BASE,
    );
  },

  async submitDocument(command: DocumentCommand): Promise<DocumentOperation> {
    requireBrowserDocumentSync();
    return request<DocumentOperation>('/documents', {
      method: 'POST',
      body: JSON.stringify(command),
    });
  },

  async getDocumentStatus(
    externalDocumentId: string,
    params: { tenantId?: string; sourceSystem?: string; sourceVersionId?: string } = {},
  ): Promise<DocumentOperation> {
    requireBrowserDocumentSync();
    return request<DocumentOperation>(
      queryPath(`/documents/${encodeURIComponent(externalDocumentId)}/status`, {
        tenantId: params.tenantId ?? HARNESS_DEFAULTS.tenantId,
        sourceSystem: params.sourceSystem ?? HARNESS_DEFAULTS.sourceSystem,
        sourceVersionId: params.sourceVersionId,
      }),
    );
  },

  async listDocumentStatus(
    params: { tenantId?: string; sourceSystem?: string; limit?: number; cursor?: string } = {},
  ): Promise<DocumentOperationPage> {
    requireBrowserDocumentSync();
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

  createConversationAttachment(
    conversationId: string,
    body: ConversationAttachmentRequest,
  ): Promise<ConversationAttachmentResponse> {
    return request<ConversationAttachmentResponse>(
      `/conversations/${encodeURIComponent(conversationId)}/attachments`,
      { method: 'POST', body: JSON.stringify(body) },
    );
  },

  issueConversationAttachmentTicket(
    attachmentId: string,
  ): Promise<ConversationAttachmentResponse> {
    return request<ConversationAttachmentResponse>(
      `/attachments/${encodeURIComponent(attachmentId)}/ticket`,
      { method: 'POST' },
    );
  },

  async verifyConversationAttachmentDownload(
    attachment: ConversationAttachmentResponse,
  ): Promise<{ contentType: string; sizeBytes: number }> {
    const origin = typeof window === 'undefined' ? 'http://localhost' : window.location.origin;
    const url = new URL(attachment.downloadUrl, origin);
    if (
      url.origin !== origin ||
      !/^\/enterprise\/api\/v2\/attachments\/[^/]+\/download\/[^/]+$/.test(url.pathname)
    ) {
      throw new V2ApiError(0, {
        code: 'ATTACHMENT_DOWNLOAD_URL_INVALID',
        message: 'Gateway returned an invalid attachment download route',
        requestId: 'attachment-download-url-invalid',
        retryable: false,
      });
    }

    let response: Response;
    try {
      response = await fetch(url.toString(), {
        headers: {
          Accept: attachment.mediaType,
          ...authHeaders(),
        },
      });
    } catch {
      throw new V2ApiError(0, {
        code: 'GATEWAY_UNAVAILABLE',
        message: 'Gateway 不可用或网络异常',
        requestId: 'gateway-unavailable',
        retryable: true,
      });
    }

    if (!response.ok) throw await errorFromResponse(response);
    const content = await response.arrayBuffer();
    return {
      contentType: response.headers.get('content-type') ?? attachment.mediaType,
      sizeBytes: content.byteLength,
    };
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
