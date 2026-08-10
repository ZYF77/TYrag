import { http, HttpResponse, delay } from 'msw';
import type {
  Conversation,
  Citation,
  FileSyncItem,
  ErrorResponse,
} from '../types';
import type {
  ConversationDetail,
  ConversationSummary,
  DocumentOperation,
  Message,
  Citation as V2Citation,
} from '../v2Types';

const BASE = '/enterprise/api/v1';

// In-memory mock data store
const conversations: Conversation[] = [
  {
    conversationId: 'conv-001',
    ragflowSessionId: 'rag-sess-001',
    createdAt: new Date(Date.now() - 3600000).toISOString(),
    title: 'AX-200 报修流程咨询',
    equipmentId: 'EQ-1001',
    fixedAssetNo: 'FA-2001',
    faultCode: null,
  },
  {
    conversationId: 'conv-002',
    ragflowSessionId: 'rag-sess-002',
    createdAt: new Date(Date.now() - 86400000).toISOString(),
    title: '设备保养周期查询',
    equipmentId: 'EQ-1002',
    fixedAssetNo: 'FA-2002',
    faultCode: 'E-104',
  },
  {
    conversationId: 'conv-003',
    ragflowSessionId: 'rag-sess-003',
    createdAt: new Date(Date.now() - 172800000).toISOString(),
    title: '液压系统故障排查',
    equipmentId: 'EQ-1003',
    fixedAssetNo: null,
    faultCode: 'H-501',
  },
];

const citations: Record<string, Citation> = {
  'cit-001': {
    citationId: 'cit-001',
    sourceType: 'document',
    title: 'AX-200 维修手册 v3.2',
    documentId: 'doc-001',
    versionId: 'ver-001',
    pageNo: 37,
    bbox: { x1: 0.1, y1: 0.2, x2: 0.8, y2: 0.4 },
    assetId: null,
    excerpt: '当设备出现 E-104 错误代码时，首先检查液压油位是否在正常范围内...',
    recordType: null,
    recordId: null,
  },
  'cit-002': {
    citationId: 'cit-002',
    sourceType: 'document',
    title: '设备操作安全规程',
    documentId: 'doc-002',
    versionId: 'ver-003',
    pageNo: 12,
    bbox: null,
    assetId: null,
    excerpt: '操作前必须确认急停按钮功能正常，安全防护装置已就位。',
    recordType: null,
    recordId: null,
  },
  'cit-003': {
    citationId: 'cit-003',
    sourceType: 'business_record',
    title: 'AX-200 最近维修记录 #WO-2024-0892',
    documentId: null,
    versionId: null,
    pageNo: null,
    bbox: null,
    assetId: 'EQ-1001',
    excerpt: '2024-11-15 更换液压泵密封件，维修人员：张工',
    recordType: 'maintenance',
    recordId: 'WO-2024-0892',
  },
};

const fileSyncItems: FileSyncItem[] = [
  {
    externalDocumentId: 'ext-doc-001',
    fileName: 'AX-200维修手册v3.2.pdf',
    status: 'ready',
    stage: null,
    error: null,
    updatedAt: new Date(Date.now() - 60000).toISOString(),
  },
  {
    externalDocumentId: 'ext-doc-002',
    fileName: '设备安全规程2024.pdf',
    status: 'ready',
    stage: null,
    error: null,
    updatedAt: new Date(Date.now() - 120000).toISOString(),
  },
  {
    externalDocumentId: 'ext-doc-003',
    fileName: '液压系统图集.pdf',
    status: 'parsing',
    stage: 'ocr_processing',
    error: null,
    updatedAt: new Date(Date.now() - 300000).toISOString(),
  },
  {
    externalDocumentId: 'ext-doc-004',
    fileName: '季度保养清单.xlsx',
    status: 'failed',
    stage: null,
    error: {
      code: 'DOCUMENT_PARSE_FAILED',
      message: '文件格式不支持，请提供 PDF 格式文件',
      requestId: 'req-fail-001',
      retryable: true,
    },
    updatedAt: new Date(Date.now() - 900000).toISOString(),
  },
];

const demoCitation: Citation = {
  citationId: 'chunk-1',
  sourceType: 'document',
  title: 'Doc1.pdf',
  documentId: 'rag-doc-1',
  versionId: 'v1',
  pageNo: 3,
  bbox: { x1: 0.1, y1: 0.2, x2: 0.8, y2: 0.4 },
  assetId: null,
  excerpt: '故障码 E-104 时先检查液压油位。',
  recordType: null,
  recordId: null,
};

interface DemoConversationRecord {
  messages: Array<{
    role: 'user' | 'assistant';
    content: string;
    citations?: Citation[];
    status?: string;
  }>;
}

const demoConversations = new Map<string, DemoConversationRecord>([
  [
    'demo-conv-existing',
    {
      messages: [
        { role: 'user', content: '历史问题' },
        {
          role: 'assistant',
          content: '历史回答',
          citations: [demoCitation],
          status: 'completed',
        },
      ],
    },
  ],
]);

function bearerToken(request: Request): string {
  return request.headers.get('authorization')?.replace(/^Bearer /i, '') ?? '';
}

function makeError(
  code: string,
  message: string,
  httpStatus: number,
  retryable = false,
): { status: number; body: ErrorResponse } {
  return {
    status: httpStatus,
    body: {
      code,
      message,
      requestId: `req-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      retryable,
    },
  };
}

export const handlers = [
  // GET /auth/me - demo identity probe
  http.get(`${BASE}/auth/me`, ({ request }) => {
    if (!bearerToken(request)) {
      const err = makeError('AUTH_TOKEN_MISSING', 'Authentication token is required', 401);
      return HttpResponse.json(err.body, { status: 401 });
    }
    return HttpResponse.json({
      businessUserId: 'demo-user',
      displayName: 'demo-user',
      tenantId: 'wp04e2e2',
      departmentIds: ['d10'],
      roles: ['end_user'],
      capabilities: ['read', 'ask', 'list_sessions', 'view_citations'],
      securityLevel: 2,
      mappingStatus: 'active',
    });
  }),

  // GET /demo/documents/:id/status
  http.get(
    `${BASE}/demo/documents/:externalDocumentId/status`,
    ({ params, request }) => {
      if (!bearerToken(request)) {
        const err = makeError('AUTH_TOKEN_MISSING', 'Authentication token is required', 401);
        return HttpResponse.json(err.body, { status: 401 });
      }
      const id = params.externalDocumentId as string;
      if (id === 'E2E-FORBIDDEN') {
        const err = makeError('ACL_DENIED', 'Access denied', 403);
        return HttpResponse.json(err.body, { status: 403 });
      }
      if (id === 'E2E-PARSING') {
        return HttpResponse.json({
          externalDocumentId: id,
          sourceVersionId: 'v1',
          ragflowDatasetId: 'ds-1',
          ragflowDocumentId: 'rag-doc-1',
          status: 'parsing',
          stage: 'ocr_processing',
          deduplicated: false,
        });
      }
      if (!['E2E-Doc1', 'E2E-Doc2'].includes(id)) {
        const err = makeError('DOCUMENT_NOT_FOUND', 'Document not found', 404);
        return HttpResponse.json(err.body, { status: 404 });
      }
      return HttpResponse.json({
        externalDocumentId: id,
        sourceVersionId: 'v1',
        ragflowDatasetId: 'ds-1',
        ragflowDocumentId: 'rag-doc-1',
        status: 'ready',
        stage: 'done',
        deduplicated: false,
      });
    },
  ),

  // POST /demo/ask
  http.post(`${BASE}/demo/ask`, async ({ request }) => {
    if (!bearerToken(request)) {
      const err = makeError('AUTH_TOKEN_MISSING', 'Authentication token is required', 401);
      return HttpResponse.json(err.body, { status: 401 });
    }
    const body = (await request.json()) as {
      externalDocumentId: string;
      question: string;
      conversationId?: string | null;
    };

    if (body.externalDocumentId === 'E2E-FORBIDDEN') {
      const err = makeError('ACL_DENIED', 'Access denied', 403);
      return HttpResponse.json(err.body, { status: 403 });
    }
    if (body.question.includes('409')) {
      const err = makeError('DOCUMENT_NOT_READY', 'Document is not ready', 409);
      return HttpResponse.json(err.body, { status: 409 });
    }
    if (body.question.includes('502')) {
      const err = makeError('RAGFLOW_SCOPE_VIOLATION', 'Retrieval scope violation', 502);
      return HttpResponse.json(err.body, { status: 502 });
    }
    if (body.question.includes('503')) {
      const err = makeError('RAGFLOW_UNAVAILABLE', 'RAGFlow unavailable', 503);
      return HttpResponse.json(err.body, { status: 503 });
    }

    const noEvidence = body.question.includes('noevidence');
    const conversationId = body.conversationId || `demo-conv-${Date.now()}`;
    const record = demoConversations.get(conversationId) ?? {
      messages: [] as DemoConversationRecord['messages'],
    };
    record.messages.push({ role: 'user', content: body.question });
    record.messages.push({
      role: 'assistant',
      content: noEvidence
        ? '未找到可靠依据，无法回答。'
        : `answer for: ${body.question}`,
      citations: noEvidence ? [] : [demoCitation],
      status: noEvidence ? 'no_reliable_evidence' : 'completed',
    });
    demoConversations.set(conversationId, record);

    return HttpResponse.json({
      answer: noEvidence
        ? '未找到可靠依据，无法回答。'
        : `answer for: ${body.question}`,
      citations: noEvidence ? [] : [demoCitation],
      conversationId,
      ragflowSessionId: 'demo-session-1',
      status: noEvidence ? 'no_reliable_evidence' : 'completed',
    });
  }),

  // GET /demo/conversations/:id
  http.get(`${BASE}/demo/conversations/:conversationId`, ({ params, request }) => {
    if (!bearerToken(request)) {
      const err = makeError('AUTH_TOKEN_MISSING', 'Authentication token is required', 401);
      return HttpResponse.json(err.body, { status: 401 });
    }
    const conversationId = params.conversationId as string;
    const record = demoConversations.get(conversationId);
    if (!record) {
      const err = makeError('CONVERSATION_NOT_FOUND', 'Conversation not found', 404);
      return HttpResponse.json(err.body, { status: 404 });
    }
    return HttpResponse.json({
      conversationId,
      ragflowSessionId: 'demo-session-1',
      messages: record.messages.map((msg, index) => ({
        messageId: `msg-${index}`,
        role: msg.role,
        content: msg.content,
        citations: msg.citations ?? [],
        status: msg.status ?? 'completed',
        createdAt: new Date().toISOString(),
      })),
    });
  }),

  // POST /conversations - create conversation
  http.post(`${BASE}/conversations`, async ({ request }) => {
    await delay(300);
    const body = (await request.json()) as {
      equipmentId?: string | null;
      fixedAssetNo?: string | null;
      faultCode?: string | null;
    };

    const conv: Conversation = {
      conversationId: `conv-${Date.now()}`,
      ragflowSessionId: `rag-sess-${Date.now()}`,
      createdAt: new Date().toISOString(),
      title: '新会话',
      equipmentId: body.equipmentId ?? null,
      fixedAssetNo: body.fixedAssetNo ?? null,
      faultCode: body.faultCode ?? null,
    };

    conversations.unshift(conv);
    return HttpResponse.json(conv, { status: 201 });
  }),

  // GET /conversations - list conversations
  http.get(`${BASE}/conversations`, async () => {
    await delay(200);
    return HttpResponse.json(conversations);
  }),

  // POST /conversations/:id/messages:stream - SSE stream
  http.post(
    `${BASE}/conversations/:conversationId/messages:stream`,
    async ({ params, request }) => {
      await delay(200);
      const body = (await request.json()) as {
        question: string;
        equipmentId?: string | null;
        faultCode?: string | null;
      };
      const question = body.question ?? '';

      // Special error modes triggered by magic questions
      if (question.includes('401')) {
        const err = makeError('AUTH_TOKEN_INVALID', '登录已过期，请重新登录', 401);
        return HttpResponse.json(err.body, { status: 401 });
      }
      if (question.includes('403')) {
        const err = makeError('ACL_DENIED', '您没有权限访问此资源', 403);
        return HttpResponse.json(err.body, { status: 403 });
      }
      if (question.includes('404')) {
        const err = makeError('CONVERSATION_NOT_FOUND', '会话不存在', 404);
        return HttpResponse.json(err.body, { status: 404 });
      }

      // Normal SSE stream
      const encoder = new TextEncoder();
      const stream = new ReadableStream({
        async start(controller) {
          const send = (event: string, data: string) => {
            controller.enqueue(encoder.encode(`event: ${event}\ndata: ${data}\n\n`));
          };

          send('run.started', JSON.stringify({ runId: 'run-001' }));
          await delay(500);

          send('retrieval.completed', JSON.stringify({}));
          await delay(300);

          // Send citations
          send('citation', JSON.stringify({ citationId: 'cit-001' }));
          await delay(200);
          send('citation', JSON.stringify({ citationId: 'cit-003' }));
          await delay(200);

          // Stream answer deltas
          const answerParts = question.includes('noevidence')
            ? []
            : [
                '根据相关文档和业务记录，',
                '以下是关于您问题的回答：\n\n',
                '**设备 AX-200** 的液压系统故障排查应按照以下步骤进行：\n\n',
                '1. 首先检查液压油位是否在正常范围内[[1]](#cit-001)；\n',
                '2. 检查液压泵工作压力是否符合规格要求；\n',
                '3. 查看最近维修记录，确认密封件更换情况[[2]](#cit-003)；\n',
                '4. 如上述步骤无法解决问题，请联系技术支持。\n\n',
                '根据维修记录 #WO-2024-0892，该设备最近一次维修中已更换液压泵密封件，',
                '建议优先排查其他可能原因。',
              ];

          if (answerParts.length === 0) {
            send('answer.completed', JSON.stringify({
              runId: 'run-001',
              status: 'no_reliable_evidence',
            }));
          } else {
            for (const part of answerParts) {
              send('answer.delta', JSON.stringify({ content: part }));
              await delay(150);
            }
            send('answer.completed', JSON.stringify({
              runId: 'run-001',
              status: 'completed',
            }));
          }

          controller.close();
        },
      });

      return new Response(stream, {
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          Connection: 'keep-alive',
          'X-Request-Id': `req-${Date.now()}`,
        },
      });
    },
  ),

  // GET /citations/:citationId
  http.get(`${BASE}/citations/:citationId`, async ({ params }) => {
    await delay(150);
    const { citationId } = params;
    const cit = citations[citationId as string];

    if (!cit) {
      const err = makeError('ACL_DENIED', '引用不存在或无权访问', 403);
      return HttpResponse.json(err.body, { status: 403 });
    }

    return HttpResponse.json(cit);
  }),

  // GET /documents/:externalDocumentId/status
  http.get(
    `${BASE}/documents/:externalDocumentId/status`,
    async ({ params }) => {
      await delay(200);
      const item = fileSyncItems.find(
        (f) => f.externalDocumentId === params.externalDocumentId,
      );
      if (!item) {
        const err = makeError('DOCUMENT_SOURCE_NOT_FOUND', '文档未找到', 404);
        return HttpResponse.json(err.body, { status: 404 });
      }

      return HttpResponse.json({
        externalDocumentId: item.externalDocumentId,
        sourceVersionId: 'ver-latest',
        ragflowDatasetId: 'ds-001',
        ragflowDocumentId: 'rag-doc-001',
        status: item.status,
        stage: item.stage,
        deduplicated: false,
        error: item.error,
      });
    },
  ),

  // GET /documents/sync-status - list all sync items
  http.get(`${BASE}/documents/sync-status`, async () => {
    await delay(200);
    return HttpResponse.json(fileSyncItems);
  }),
];

// ---- v2 Harness fixtures -------------------------------------------------
// These handlers are deliberately separate from the v1 compatibility fixtures.
// They only expose the frozen v2 external response shape.

const V2_BASE = '/enterprise/api/v2';
const v2Documents = new Map<string, DocumentOperation>();
const v2DocumentPolls = new Map<string, number>();
const v2DocumentPayloadHashes = new Map<string, string>();
const v2Conversations = new Map<string, { detail: ConversationDetail; messages: Message[] }>();
const v2Runs = new Map<string, { result: V2MessageRunResult; messages: Message[]; question: string; pending: boolean }>();

const canonicalJson = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    return `{${Object.keys(value as Record<string, unknown>)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson((value as Record<string, unknown>)[key])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
};

interface V2MessageRunResult {
  conversationId: string;
  clientMessageId: string;
  runId: string;
  messageId: string;
  answer: string;
  status: 'completed' | 'no_reliable_evidence' | 'failed';
  citations: V2Citation[];
  replayed: boolean;
}

const v2Citation: V2Citation = {
  citationId: 'v2-cit-001',
  sourceType: 'document',
  title: 'Harness maintenance manual',
  externalDocumentId: 'HARNESS-DOC-001',
  sourceVersionId: 'v1',
  pageNo: 3,
  bbox: { x1: 0.1, y1: 0.2, x2: 0.8, y2: 0.4 },
  assetId: 'ASSET-HARNESS-001',
  excerpt: '检查液压油位并确认维护步骤。',
  recordType: null,
  recordId: null,
};

function v2Now(): string {
  return new Date().toISOString();
}

function v2Error(code: string, message: string, status: number, retryable = false): Response {
  return HttpResponse.json(
    {
      code,
      message,
      requestId: `v2-${code.toLowerCase()}-${Date.now()}`,
      retryable,
    },
    { status },
  );
}

function v2AuthError(request: Request): Response | null {
  const token = request.headers.get('authorization')?.replace(/^Bearer\s+/i, '') ?? '';
  if (token.toLowerCase().includes('401') || token.toLowerCase().includes('invalid')) {
    return v2Error('AUTH_TOKEN_INVALID', '登录已过期，请重新注入运行期 Bearer。', 401);
  }
  return null;
}

function v2ConversationDetail(id: string, body: { equipmentId?: string | null; fixedAssetNo?: string | null; faultCode?: string | null }): ConversationDetail {
  const now = v2Now();
  const hasContext = Boolean(body.equipmentId || body.fixedAssetNo || body.faultCode);
  return {
    conversationId: id,
    title: 'Harness 会话',
    status: 'active',
    equipmentId: body.equipmentId ?? null,
    fixedAssetNo: body.fixedAssetNo ?? null,
    faultCode: body.faultCode ?? null,
    contextVersion: hasContext ? 1 : 0,
    lastMessageAt: now,
    createdAt: now,
    context: {
      equipmentId: body.equipmentId ?? null,
      fixedAssetNo: body.fixedAssetNo ?? null,
      faultCode: body.faultCode ?? null,
      contextVersion: hasContext ? 1 : 0,
      registryVersion: hasContext ? 'mock-registry-v1' : null,
    },
  };
}

function v2ScenarioError(value: string): Response | null {
  if (value.includes('401')) return v2Error('AUTH_TOKEN_INVALID', 'Authentication token is invalid', 401);
  if (value.includes('403')) return v2Error('ACL_DENIED', 'Access denied', 403);
  if (value.includes('409')) return v2Error('CONVERSATION_CONTEXT_STALE', 'Conversation context is stale', 409);
  if (value.includes('422')) return v2Error('VALIDATION_ERROR', 'Request validation failed', 422);
  if (value.toLowerCase().includes('asset-registry')) return v2Error('ASSET_REGISTRY_UNAVAILABLE', 'Asset Registry is temporarily unavailable', 503, true);
  if (value.includes('503')) return v2Error('RAGFLOW_UNAVAILABLE', 'RAGFlow service is temporarily unavailable', 503, true);
  return null;
}

function v2Stream(
  result: V2MessageRunResult,
  includeFailure: boolean,
): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    async start(controller) {
      const send = (event: string, data: unknown) => {
        controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      };
      send('run.started', {
        conversationId: result.conversationId,
        clientMessageId: result.clientMessageId,
        runId: result.runId,
        replayed: result.replayed,
      });
      await delay(10);
      if (includeFailure) {
        send('run.failed', {
          conversationId: result.conversationId,
          runId: result.runId,
          code: 'RAGFLOW_UNAVAILABLE',
          message: 'RAGFlow service is temporarily unavailable',
        });
        controller.close();
        return;
      }
      if (result.answer) {
        send('answer.delta', { conversationId: result.conversationId, runId: result.runId, content: result.answer.slice(0, Math.ceil(result.answer.length / 2)) });
        await delay(10);
        send('answer.delta', { conversationId: result.conversationId, runId: result.runId, content: result.answer.slice(Math.ceil(result.answer.length / 2)) });
      }
      for (const citation of result.citations) {
        await delay(10);
        send('citation', citation);
      }
      send('answer.completed', {
        conversationId: result.conversationId,
        runId: result.runId,
        messageId: result.messageId,
        status: result.status,
        citations: result.citations,
      });
      controller.close();
    },
  });
  return new Response(stream, {
    headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' },
  });
}

const v2Handlers = [
  http.post(`${V2_BASE}/documents`, async ({ request }) => {
    const authError = v2AuthError(request);
    if (authError) return authError;
    const body = (await request.json()) as { eventId?: string; externalDocumentId?: string; sourceVersionId?: string; sha256?: string; tenantId?: string; sourceSystem?: string; metadata?: Record<string, unknown> };
    const scenario = v2ScenarioError(body.externalDocumentId ?? body.eventId ?? '');
    if (scenario) return scenario;
    if (!body.sha256 || !/^[0-9a-fA-F]{64}$/.test(body.sha256) || !body.metadata) {
      return v2Error('DOCUMENT_METADATA_INVALID', 'Metadata validation failed', 422);
    }
    const externalDocumentId = body.externalDocumentId ?? 'HARNESS-DOC-001';
    const payloadHash = canonicalJson(body);
    const previousPayloadHash = body.eventId ? v2DocumentPayloadHashes.get(body.eventId) : undefined;
    if (previousPayloadHash !== undefined && previousPayloadHash !== payloadHash) {
      return v2Error('EVENT_PAYLOAD_CONFLICT', 'The eventId was already used with a different payload', 409);
    }
    const operation: DocumentOperation = {
      operationId: body.eventId ?? `op-${Date.now()}`,
      externalDocumentId,
      sourceVersionId: body.sourceVersionId ?? 'v1',
      status: 'received',
      stage: 'accepted',
      deduplicated: v2Documents.has(externalDocumentId),
      businessStatus: 'active',
      currentVersion: true,
      eventStatus: 'accepted',
      updatedAt: v2Now(),
    };
    v2Documents.set(externalDocumentId, operation);
    if (body.eventId) v2DocumentPayloadHashes.set(body.eventId, payloadHash);
    v2DocumentPolls.set(externalDocumentId, 0);
    return HttpResponse.json(operation, { status: 202 });
  }),

  http.get(`${V2_BASE}/documents/sync-status`, ({ request }) => {
    const authError = v2AuthError(request);
    if (authError) return authError;
    return HttpResponse.json({ items: [...v2Documents.values()], nextCursor: null, hasMore: false });
  }),

  http.get(`${V2_BASE}/documents/:externalDocumentId/status`, ({ params, request }) => {
    const authError = v2AuthError(request);
    if (authError) return authError;
    const id = String(params.externalDocumentId);
    const scenario = v2ScenarioError(id);
    if (scenario) return scenario;
    const operation = v2Documents.get(id);
    if (!operation) return v2Error('DOCUMENT_NOT_FOUND', 'Document not found', 404);
    const polls = (v2DocumentPolls.get(id) ?? 0) + 1;
    v2DocumentPolls.set(id, polls);
    if (polls >= 2 && operation.status !== 'ready') {
      operation.status = 'ready';
      operation.stage = 'complete';
      operation.eventStatus = 'processed';
      operation.updatedAt = v2Now();
    }
    return HttpResponse.json(operation);
  }),

  http.post(`${V2_BASE}/conversations`, async ({ request }) => {
    const authError = v2AuthError(request);
    if (authError) return authError;
    const body = (await request.json()) as { equipmentId?: string | null; fixedAssetNo?: string | null; faultCode?: string | null };
    const scenario = v2ScenarioError(`${body.equipmentId ?? ''}${body.faultCode ?? ''}`);
    if (scenario) return scenario;
    const id = `v2-conv-${Date.now()}`;
    const detail = v2ConversationDetail(id, body);
    v2Conversations.set(id, { detail, messages: [] });
    return HttpResponse.json(detail, { status: 201 });
  }),

  http.get(`${V2_BASE}/conversations`, ({ request }) => {
    const authError = v2AuthError(request);
    if (authError) return authError;
    const items: ConversationSummary[] = [...v2Conversations.values()].map(({ detail }) => ({ ...detail }));
    return HttpResponse.json({ items, nextCursor: null, hasMore: false });
  }),

  http.get(`${V2_BASE}/conversations/:conversationId`, ({ params, request }) => {
    const authError = v2AuthError(request);
    if (authError) return authError;
    const id = String(params.conversationId);
    const record = v2Conversations.get(id);
    if (!record) return v2Error('CONVERSATION_NOT_FOUND', 'Conversation not found', 404);
    return HttpResponse.json(record.detail);
  }),

  http.patch(`${V2_BASE}/conversations/:conversationId/context`, async ({ params, request }) => {
    const authError = v2AuthError(request);
    if (authError) return authError;
    const body = (await request.json()) as { equipmentId?: string | null; fixedAssetNo?: string | null; faultCode?: string | null };
    const scenario = v2ScenarioError(`${body.equipmentId ?? ''}${body.faultCode ?? ''}`);
    if (scenario) return scenario;
    const record = v2Conversations.get(String(params.conversationId));
    if (!record) return v2Error('CONVERSATION_NOT_FOUND', 'Conversation not found', 404);
    const old = record.detail;
    const next = {
      equipmentId: body.equipmentId === undefined ? old.equipmentId : body.equipmentId,
      fixedAssetNo: body.fixedAssetNo === undefined ? old.fixedAssetNo : body.fixedAssetNo,
      faultCode: body.faultCode === undefined ? old.faultCode : body.faultCode,
    };
    const changed = next.equipmentId !== old.equipmentId || next.fixedAssetNo !== old.fixedAssetNo || next.faultCode !== old.faultCode;
    const contextVersion = old.contextVersion + (changed ? 1 : 0);
    const detail: ConversationDetail = {
      ...old,
      ...next,
      contextVersion,
      context: { ...old.context, ...next, contextVersion, registryVersion: next.equipmentId ? 'mock-registry-v1' : null },
    };
    record.detail = detail;
    return HttpResponse.json(detail);
  }),

  http.get(`${V2_BASE}/conversations/:conversationId/messages`, ({ params, request }) => {
    const authError = v2AuthError(request);
    if (authError) return authError;
    const record = v2Conversations.get(String(params.conversationId));
    if (!record) return v2Error('CONVERSATION_NOT_FOUND', 'Conversation not found', 404);
    return HttpResponse.json({ items: record.messages, nextCursor: null, hasMore: false });
  }),

  http.post(`${V2_BASE}/conversations/:conversationId/messages`, async ({ params, request }) => {
    const authError = v2AuthError(request);
    if (authError) return authError;
    const id = String(params.conversationId);
    const record = v2Conversations.get(id);
    if (!record) return v2Error('CONVERSATION_NOT_FOUND', 'Conversation not found', 404);
    const body = (await request.json()) as { clientMessageId?: string; question?: string };
    const question = body.question ?? '';
    const scenario = v2ScenarioError(question);
    if (scenario) return scenario;
    if (!body.clientMessageId || !question) return v2Error('VALIDATION_ERROR', 'Request validation failed', 422);
    const runKey = `${id}:${body.clientMessageId}`;
    const existing = v2Runs.get(runKey);
    if (existing) {
      if (existing.question !== question) {
        return v2Error('MESSAGE_PAYLOAD_CONFLICT', 'The clientMessageId was already used with a different question', 409);
      }
      if (existing.pending) {
        existing.pending = false;
        return HttpResponse.json({ ...existing.result, status: 'running', replayed: true }, { status: 202 });
      }
      const replayed = { ...existing.result, replayed: true };
      if ((request.headers.get('accept') ?? '').includes('text/event-stream')) return v2Stream(replayed, false);
      return HttpResponse.json(replayed);
    }
    const noEvidence = question.toLowerCase().includes('noevidence');
    const streamFailure = question.includes('sse-error');
    const answer = noEvidence ? '未找到可靠依据，无法回答。' : `Harness answer: ${question}`;
    const citations = noEvidence ? [] : [v2Citation];
    const result: V2MessageRunResult = {
      conversationId: id,
      clientMessageId: body.clientMessageId,
      runId: `run-${Date.now()}`,
      messageId: `message-${Date.now()}`,
      answer,
      status: streamFailure ? 'failed' : noEvidence ? 'no_reliable_evidence' : 'completed',
      citations,
      replayed: false,
    };
    const userMessage: Message = { messageId: `${result.messageId}-user`, role: 'user', content: question, status: 'completed', citations: [], createdAt: v2Now() };
    const assistantMessage: Message = { messageId: result.messageId, role: 'assistant', content: answer, status: result.status, citations, createdAt: v2Now() };
    const storedMessages = [...record.messages, userMessage, assistantMessage];
    record.messages = storedMessages;
    record.detail = { ...record.detail, lastMessageAt: v2Now(), title: question.slice(0, 40) || record.detail.title };
    const pending = question.toLowerCase().includes('pending');
    v2Runs.set(runKey, { result, messages: storedMessages, question, pending });
    if (pending) return HttpResponse.json({ conversationId: id, clientMessageId: body.clientMessageId, runId: result.runId, status: 'running', replayed: true }, { status: 202 });
    if ((request.headers.get('accept') ?? '').includes('text/event-stream')) return v2Stream(result, question.includes('sse-error'));
    return HttpResponse.json(result);
  }),

  http.get(`${V2_BASE}/citations/:citationId`, ({ params, request }) => {
    const authError = v2AuthError(request);
    if (authError) return authError;
    if (String(params.citationId) === v2Citation.citationId) return HttpResponse.json(v2Citation);
    return v2Error('CITATION_NOT_FOUND', 'Citation not found', 404);
  }),

  http.post(`${V2_BASE}/conversations/:conversationId/attachments`, async ({ params, request }) => {
    const authError = v2AuthError(request);
    if (authError) return authError;
    if (!v2Conversations.has(String(params.conversationId))) {
      return v2Error('CONVERSATION_NOT_FOUND', 'Conversation not found', 404);
    }
    const body = (await request.json()) as { fileName?: string };
    if (body.fileName?.toLowerCase().includes('expired')) {
      return v2Error('ATTACHMENT_EXPIRED', 'Transient attachment has expired', 404);
    }
    return v2Error('ATTACHMENT_NOT_IMPLEMENTED', 'Transient attachment is planned but not enabled', 501);
  }),
];

handlers.push(...v2Handlers);
