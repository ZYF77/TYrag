import { describe, it, expect, beforeEach } from 'vitest';
import { setupServer } from 'msw/node';
import { handlers } from '../api/mocks/handlers';
import { api } from '../api/client';

const server = setupServer(...handlers);

beforeEach(() => {
  server.resetHandlers();
});

describe('API Client', () => {
  describe('createConversation', () => {
    it('creates a conversation and returns it', async () => {
      const conv = await api.createConversation({
        equipmentId: 'EQ-1001',
      });
      expect(conv.conversationId).toBeTruthy();
      expect(conv.ragflowSessionId).toBeTruthy();
      expect(conv.equipmentId).toBe('EQ-1001');
    });

    it('creates a conversation without context', async () => {
      const conv = await api.createConversation();
      expect(conv.conversationId).toBeTruthy();
      expect(conv.equipmentId).toBeNull();
    });
  });

  describe('listConversations', () => {
    it('returns conversation list', async () => {
      const list = await api.listConversations();
      expect(list.length).toBeGreaterThanOrEqual(3);
      expect(list[0].conversationId).toBeTruthy();
    });
  });

  describe('getCitation', () => {
    it('returns citation details', async () => {
      const cit = await api.getCitation('cit-001');
      expect(cit.citationId).toBe('cit-001');
      expect(cit.sourceType).toBe('document');
      expect(cit.title).toBe('AX-200 维修手册 v3.2');
    });

    it('throws 403 for unknown citation', async () => {
      await expect(api.getCitation('unknown')).rejects.toThrow();
    });
  });

  describe('getDocumentStatus', () => {
    it('returns ready status for known doc', async () => {
      const status = await api.getDocumentStatus('ext-doc-001');
      expect(status.status).toBe('ready');
    });

    it('returns parsing status with stage', async () => {
      const status = await api.getDocumentStatus('ext-doc-003');
      expect(status.status).toBe('parsing');
      expect(status.stage).toBe('ocr_processing');
    });

    it('returns failed status with error', async () => {
      const status = await api.getDocumentStatus('ext-doc-004');
      expect(status.status).toBe('failed');
      expect(status.error).toBeTruthy();
    });

    it('throws 404 for unknown doc', async () => {
      await expect(api.getDocumentStatus('unknown')).rejects.toThrow();
    });
  });

  describe('listSyncStatus', () => {
    it('returns sync status list', async () => {
      const items = await api.listSyncStatus();
      expect(items.length).toBeGreaterThanOrEqual(4);
    });
  });
});
