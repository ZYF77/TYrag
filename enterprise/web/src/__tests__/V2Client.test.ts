import { describe, expect, it } from 'vitest';
import { V2ApiError, v2Api } from '../api/v2Client';

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
    await expect(v2Api.submitDocument({ ...command, sourceVersionId: 'v2' })).rejects.toMatchObject({ status: 409 });
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
    expect(assistant?.status).toBe('failed');
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
    await expect(second.promise).rejects.toMatchObject({ status: 409 });
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

  it('uses the planned attachment route and preserves expiry/not-implemented errors', async () => {
    const conversation = await v2Api.createConversation({ equipmentId: 'EQ-ATTACHMENT' });
    const body = { fileName: 'manual.pdf', mediaType: 'application/pdf', content: 'cGRm' };
    await expect(v2Api.createConversationAttachment(conversation.conversationId, body)).rejects.toMatchObject({
      status: 501,
      body: { code: 'ATTACHMENT_NOT_IMPLEMENTED' },
    });
    await expect(v2Api.createConversationAttachment(conversation.conversationId, { ...body, fileName: 'expired.pdf' })).rejects.toMatchObject({
      status: 404,
      body: { code: 'ATTACHMENT_EXPIRED' },
    });
  });
});
