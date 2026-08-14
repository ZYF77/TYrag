import type { DisplayError } from './v2Types';

export type ConsoleModuleStatus =
  | 'configured'
  | 'healthy'
  | 'unavailable'
  | 'unauthorized'
  | 'processing'
  | 'retrievable'
  | 'failed';

export interface ConsoleState<T> {
  status: ConsoleModuleStatus;
  data: T | null;
  error: DisplayError | null;
}

export interface GatewayHealth {
  status: string;
  version: string;
}

export interface ConsoleUserPrincipal {
  displayName: string;
  tenantId: string;
  roles: string[];
  capabilities: string[];
  mappingStatus: string;
}

export interface FileShareReadiness {
  currentVersion: boolean;
  active: boolean;
  syncReady: boolean;
  parserReadback: boolean;
  ragflowIdsPresent: boolean;
  qualityPassed: boolean;
  blockingReason: string | null;
}

export interface FileShareDocumentStatus {
  externalDocumentId: string;
  sourceVersionId: string;
  status: string;
  stage: string | null;
  pipelineStatus: string | null;
  parseCompleted: boolean;
  indexCompleted: boolean;
  ingestState: string;
  sourceState: string;
  deduplicated: boolean;
  businessStatus: string;
  currentVersion: boolean;
  eventStatus: string;
  updatedAt: string;
  retrievable: boolean;
  readiness: FileShareReadiness;
  qualityStatus: string | null;
  errorCode: string | null;
  error: { code: string; message: string; retryable: boolean } | null;
}

export interface FileShareDocumentStatusPage {
  items: FileShareDocumentStatus[];
}

export interface GatewayHttpLogEvent {
  id: string;
  ts: string;
  direction: 'inbound' | 'outbound' | string;
  kind: string;
  method: string;
  path: string;
  query?: string;
  http_status: number | null;
  duration_ms?: number;
  body?: unknown;
  response_body?: unknown;
  streamed?: boolean;
  outcome?: string | null;
  error?: string | null;
}

export interface GatewayHttpLogPage {
  items: GatewayHttpLogEvent[];
}
