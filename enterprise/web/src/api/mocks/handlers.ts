import { http, HttpResponse, delay } from 'msw';
import type {
  Conversation,
  Citation,
  FileSyncItem,
  ErrorResponse,
} from '../types';

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
              status: 'no_evidence',
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
