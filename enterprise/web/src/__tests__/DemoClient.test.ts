import { describe, it, expect, beforeEach } from 'vitest';
import { demoApi, setDemoToken } from '../api/demoClient';

describe('demoApi', () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
    setDemoToken('test-jwt');
  });

  it('queries a ready document status with bearer auth', async () => {
    const status = await demoApi.getDocumentStatus('E2E-Doc1');
    expect(status.externalDocumentId).toBe('E2E-Doc1');
    expect(status.status).toBe('ready');
  });

  it('asks a question and returns answer, citations and conversation id', async () => {
    const result = await demoApi.ask({
      externalDocumentId: 'E2E-Doc1',
      question: '故障码 E-104 怎么处理？',
    });
    expect(result.answer).toContain('answer for:');
    expect(result.conversationId).toBeTruthy();
    expect(result.status).toBe('completed');
    expect(result.citations[0].title).toBe('Doc1.pdf');
    expect(result.citations[0].documentId).toBe('rag-doc-1');
  });

  it('returns no reliable evidence status without deriving it from citations', async () => {
    const result = await demoApi.ask({
      externalDocumentId: 'E2E-Doc1',
      question: 'noevidence query',
    });
    expect(result.status).toBe('no_reliable_evidence');
    expect(result.answer).toBe('未找到可靠依据，无法回答。');
    expect(result.citations).toEqual([]);
  });

  it('continues an existing conversation with the same conversation id', async () => {
    const result = await demoApi.ask({
      externalDocumentId: 'E2E-Doc1',
      question: '第二轮',
      conversationId: 'demo-conv-existing',
    });
    expect(result.conversationId).toBe('demo-conv-existing');
  });

  it('restores a persisted conversation history', async () => {
    const conversation = await demoApi.getConversation('demo-conv-existing');
    expect(conversation.messages[0].content).toBe('历史问题');
    expect(conversation.messages[1].citations[0].pageNo).toBe(3);
  });

  it('maps 409 to DOCUMENT_NOT_READY', async () => {
    await expect(
      demoApi.ask({
        externalDocumentId: 'E2E-Doc1',
        question: '409 提前提问',
      }),
    ).rejects.toMatchObject({
      status: 409,
      body: { code: 'DOCUMENT_NOT_READY' },
    });
  });

  it('maps 502 to RAGFLOW_SCOPE_VIOLATION', async () => {
    await expect(
      demoApi.ask({
        externalDocumentId: 'E2E-Doc1',
        question: '502 scope',
      }),
    ).rejects.toMatchObject({
      status: 502,
      body: { code: 'RAGFLOW_SCOPE_VIOLATION' },
    });
  });

  it('maps 503 to RAGFLOW_UNAVAILABLE', async () => {
    await expect(
      demoApi.ask({
        externalDocumentId: 'E2E-Doc1',
        question: '503 unavailable',
      }),
    ).rejects.toMatchObject({
      status: 503,
      body: { code: 'RAGFLOW_UNAVAILABLE' },
    });
  });
});
