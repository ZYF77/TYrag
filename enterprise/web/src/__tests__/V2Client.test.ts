import { describe, expect, it, vi } from 'vitest';
import { MESSAGE_FILE_LIMITS, V2ApiError, v2Api } from '../api/v2Client';

const command = {
  eventId: 'test-event-v2',
  eventType: 'upsert' as const,
  tenantId: 'demo-tenant',
  sourceSystem: 'equipment-system',
  externalDocumentId: 'TEST-DOC-V2',
  sourceVersionId: 'v1',
  sha256: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
  fileName: 'test.pdf',
  mediaType: 'application/pdf',
  source: { bucket: 'test-bucket', objectKey: 'test.pdf' },
  metadata: {
    schema_version: 1 as const,
    tenant_id: 'demo-tenant',
    source_system: 'equipment-system',
    external_document_id: 'TEST-DOC-V2',
    equipment_id: 'EQ-1001',
    fixed_asset_no: null,
    asset_id: null,
    document_type: 'manual',
    document_version: 'v1',
    department_id: 'maintenance',
    security_level: 2,
    business_status: 'active' as const,
  },
};

describe('v2 API client', () => {
  it('does not issue browser document requests in gateway mode', async () => {
    vi.stubEnv('VITE_API_MODE', 'gateway');
    vi.resetModules();
    try {
      const { v2Api: gatewayApi } = await import('../api/v2Client');
      await expect(gatewayApi.submitDocument(command)).rejects.toMatchObject({
        status: 0,
        body: { code: 'DOCUMENT_PRODUCER_REQUIRED' },
      });
    } finally {
      vi.unstubAllEnvs();
      vi.resetModules();
    }
  });

  it('submits and polls external document status without engine identifiers', async () => {
    const accepted = await v2Api.submitDocument(command);
    expect(accepted.externalDocumentId).toBe('TEST-DOC-V2');
    expect(JSON.stringify(accepted).toLowerCase()).not.toContain('ragflow');

    await v2Api.getDocumentStatus('TEST-DOC-V2', { tenantId: 'demo-tenant', sourceSystem: 'equipment-system' });
    const ready = await v2Api.getDocumentStatus('TEST-DOC-V2', { tenantId: 'demo-tenant', sourceSystem: 'equipment-system' });
    expect(ready.status).toBe('ready');
    expect(ready.stage).toBe('complete');
  });

  it('rejects an eventId reused with a different payload', async () => {
    await v2Api.submitDocument(command);
    await expect(v2Api.submitDocument({ ...command, sourceVersionId: 'v2' })).rejects.toMatchObject({
      status: 409,
      body: { code: 'EVENT_ID_CONFLICT' },
    });
  });

  it('replays an event when equivalent JSON fields use a different order', async () => {
    const eventId = 'test-event-canonical-v2';
    await v2Api.submitDocument({ ...command, eventId });
    const reordered = {
      ...command,
      eventId,
      metadata: {
        business_status: 'active' as const,
        security_level: 2,
        department_id: 'maintenance',
        document_version: 'v1',
        document_type: 'manual',
        asset_id: null,
        fixed_asset_no: null,
        equipment_id: 'EQ-1001',
        external_document_id: 'TEST-DOC-V2',
        source_system: 'equipment-system',
        tenant_id: 'demo-tenant',
        schema_version: 1 as const,
      },
    };

    await expect(v2Api.submitDocument(reordered)).resolves.toMatchObject({ externalDocumentId: 'TEST-DOC-V2' });
  });

  it('creates a conversation, switches context, and keeps the response external-only', async () => {
    const conversation = await v2Api.createConversation({ equipmentId: 'EQ-1001', fixedAssetNo: 'FA-2001' });
    expect(conversation.context.equipmentId).toBe('EQ-1001');
    expect(conversation.context.fixedAssetNo).toBe('FA-2001');
    expect(conversation.contextVersion).toBe(1);
    const updated = await v2Api.patchConversationContext(conversation.conversationId, { faultCode: 'E-104' });
    expect(updated.faultCode).toBe('E-104');
    const page = await v2Api.listConversations();
    expect(page.items.some((item) => item.conversationId === conversation.conversationId)).toBe(true);
    expect(JSON.stringify(updated).toLowerCase()).not.toContain('ragflow');
  });

  it('uses the existing messages route with true SSE and supports replay', async () => {
    const conversation = await v2Api.createConversation({ equipmentId: 'EQ-1001' });
    const first: Array<{ event: string; data: string }> = [];
    const firstStream = v2Api.streamMessage(
      conversation.conversationId,
      { clientMessageId: 'client-replay-v2', question: 'how to inspect?' },
      (event) => first.push(event),
    );
    await firstStream.promise;
    expect(first.some((event) => event.event === 'answer.delta')).toBe(true);
    expect(first.some((event) => event.event === 'answer.completed')).toBe(true);
    expect(first.some((event) => event.event === 'citation')).toBe(true);

    const replay: Array<{ event: string; data: string }> = [];
    const replayStream = v2Api.streamMessage(
      conversation.conversationId,
      { clientMessageId: 'client-replay-v2', question: 'how to inspect?' },
      (event) => replay.push(event),
    );
    await replayStream.promise;
    expect(replay[0].event).toBe('run.started');
    expect(JSON.parse(replay[0].data).replayed).toBe(true);
  });

  it('replays persisted business status and citations without deriving one from the other', async () => {
    const conversation = await v2Api.createConversation({ equipmentId: 'EQ-REPLAY-STATUS' });
    const stream = v2Api.streamMessage(
      conversation.conversationId,
      { clientMessageId: 'client-failed-replay', question: 'sse-error replay' },
      () => undefined,
    );
    await stream.promise;

    const history = await v2Api.listMessages(conversation.conversationId);
    const assistant = history.items.find((item) => item.role === 'assistant');
    expect(assistant?.status).toBe('失败');
    expect(assistant?.citations).toHaveLength(1);
    expect(assistant?.citations[0].assetId).toBe('ASSET-HARNESS-001');
  });

  it('rejects a reused clientMessageId when the question changes', async () => {
    const conversation = await v2Api.createConversation({ equipmentId: 'EQ-CONFLICT' });
    const first = v2Api.streamMessage(
      conversation.conversationId,
      { clientMessageId: 'client-conflict-v2', question: 'first question' },
      () => undefined,
    );
    await first.promise;
    const second = v2Api.streamMessage(
      conversation.conversationId,
      { clientMessageId: 'client-conflict-v2', question: 'different question' },
      () => undefined,
    );
    await expect(second.promise).rejects.toMatchObject({
      status: 409,
      body: { code: 'CLIENT_MESSAGE_ID_CONFLICT' },
    });
  });

  it('polls a 202 pending run until the durable result is available', async () => {
    const conversation = await v2Api.createConversation({ equipmentId: 'EQ-PENDING' });
    const events: Array<{ event: string; data: string }> = [];
    const stream = v2Api.streamMessage(
      conversation.conversationId,
      { clientMessageId: 'client-pending-v2', question: 'pending question' },
      (event) => events.push(event),
    );
    await stream.promise;
    expect(events.some((event) => event.event === 'answer.completed')).toBe(true);
  });

  it('sends question files as multipart metadata + files and persists attachment history', async () => {
    const conversation = await v2Api.createConversation({ equipmentId: 'EQ-FILES' });
    const events: Array<{ event: string; data: string }> = [];
    const stream = v2Api.streamMessage(
      conversation.conversationId,
      { clientMessageId: 'client-files-v2', question: 'file question' },
      (event) => events.push(event),
      [new File(['file-content'], 'note.txt', { type: 'text/plain' })],
    );
    await stream.promise;
    expect(events.some((event) => event.event === 'run.started')).toBe(true);
    expect(events.some((event) => event.event === 'answer.completed')).toBe(true);
    const answer = events
      .filter((event) => event.event === 'answer.delta')
      .map((event) => (JSON.parse(event.data) as { content?: string }).content ?? '')
      .join('');
    expect(answer).toBe('Harness answer: file question');

    const history = await v2Api.listMessages(conversation.conversationId);
    const user = history.items.find((item) => item.role === 'user');
    expect(user?.attachments?.map((item) => item.fileName)).toEqual(['note.txt']);
    expect(user?.attachments?.[0].mediaType).toBe('text/plain');
  });

  it('rejects invalid message files before any request is sent', async () => {
    const conversation = await v2Api.createConversation({ equipmentId: 'EQ-FILELIMIT' });
    const body = { clientMessageId: 'client-file-limit', question: 'q' } as const;
    const onEvent = () => undefined;
    const tooMany = Array.from(
      { length: MESSAGE_FILE_LIMITS.maxFiles + 1 },
      (_, index) => new File(['x'], `f${index}.txt`, { type: 'text/plain' }),
    );
    await expect(
      v2Api.streamMessage(conversation.conversationId, body, onEvent, tooMany).promise,
    ).rejects.toMatchObject({ status: 0, body: { code: 'MESSAGE_FILES_INVALID' } });

    const tooBig = [new File([new ArrayBuffer(MESSAGE_FILE_LIMITS.maxFileBytes + 1)], 'big.pdf', { type: 'application/pdf' })];
    await expect(
      v2Api.streamMessage(conversation.conversationId, body, onEvent, tooBig).promise,
    ).rejects.toMatchObject({ status: 0, body: { code: 'MESSAGE_FILES_INVALID' } });

    const badType = [new File(['x'], 'evil.zip', { type: 'application/zip' })];
    await expect(
      v2Api.streamMessage(conversation.conversationId, body, onEvent, badType).promise,
    ).rejects.toMatchObject({ status: 0, body: { code: 'MESSAGE_FILES_INVALID' } });

    const empty = [new File([], 'empty.txt', { type: 'text/plain' })];
    await expect(
      v2Api.streamMessage(conversation.conversationId, body, onEvent, empty).promise,
    ).rejects.toMatchObject({ status: 0, body: { code: 'MESSAGE_FILES_INVALID' } });
  });

  it.each([401, 403, 409, 422, 503])('retains HTTP %s for diagnostics', async (status) => {
    await expect(v2Api.createConversation({ equipmentId: `scenario-${status}` })).rejects.toMatchObject({ status });
    try {
      await v2Api.createConversation({ equipmentId: `scenario-${status}` });
    } catch (error) {
      expect(error).toBeInstanceOf(V2ApiError);
      expect((error as V2ApiError).body.code).toBeTruthy();
    }
  });

  it('retains the Asset Registry unavailable contract error', async () => {
    await expect(v2Api.createConversation({ equipmentId: 'asset-registry' })).rejects.toMatchObject({
      status: 503,
      body: { code: 'ASSET_REGISTRY_UNAVAILABLE', retryable: true },
    });
  });

  it('uses the public create/ticket/download attachment routes and preserves expiry errors', async () => {
    const conversation = await v2Api.createConversation({ equipmentId: 'EQ-ATTACHMENT' });
    const body = { fileName: 'manual.pdf', mediaType: 'application/pdf', content: 'cGRm' };
    const created = await v2Api.createConversationAttachment(conversation.conversationId, body);
    expect(created.indexPolicy).toBe('never');
    expect(created.downloadUrl).toContain('/enterprise/api/v2/attachments/');
    const ticketed = await v2Api.issueConversationAttachmentTicket(created.attachmentId);
    expect(ticketed.ticketExpiresAt).toBeTruthy();
    await expect(v2Api.verifyConversationAttachmentDownload(ticketed)).resolves.toMatchObject({
      contentType: 'application/pdf',
      sizeBytes: 21,
    });
    await expect(v2Api.verifyConversationAttachmentDownload(ticketed)).rejects.toMatchObject({
      status: 404,
      body: { code: 'ATTACHMENT_TICKET_INVALID' },
    });
    await expect(v2Api.createConversationAttachment(conversation.conversationId, { ...body, fileName: 'expired.pdf' })).rejects.toMatchObject({
      status: 410,
      body: { code: 'ATTACHMENT_EXPIRED' },
    });
    await expect(v2Api.createConversationAttachment(conversation.conversationId, { ...body, fileName: 'forbidden.pdf' })).rejects.toMatchObject({
      status: 403,
      body: { code: 'ATTACHMENT_FORBIDDEN' },
    });
  });
});
