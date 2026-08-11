import type { ApiMode } from './mode';

/** Browser document calls are intentionally limited to the non-Integration mock. */
export function browserDocumentSyncEnabled(mode: ApiMode): boolean {
  return mode === 'mock';
}
