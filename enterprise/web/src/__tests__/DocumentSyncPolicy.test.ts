import { describe, expect, it } from 'vitest';
import { browserDocumentSyncEnabled } from '../api/documentSyncPolicy';

describe('document sync security boundary', () => {
  it('allows document UI calls only for the explicitly non-Integration mock mode', () => {
    expect(browserDocumentSyncEnabled('mock')).toBe(true);
    expect(browserDocumentSyncEnabled('demo')).toBe(false);
    expect(browserDocumentSyncEnabled('gateway')).toBe(false);
  });
});
