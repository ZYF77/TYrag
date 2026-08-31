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

export interface GatewayHttpLogEvent {
  id: string;
  ts: string;
  direction: 'inbound' | 'outbound' | string;
  kind: string;
  method: string;
  path: string;
  query?: string;
  caller?: string | null;
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

// ---- Admin system settings (integrations + metadata) ----------------------

/** Callback binding discriminator; "EAM" is the only value the backend registers today. */
export type CallbackBinding = string;

export interface RagflowIntegrationPaths {
  health: string;
  datasets: string;
  chats: string;
  completions: string;
  retrieval: string;
}

export interface RagflowIntegration {
  baseUrl: string;
  apiVersion: string;
  paths: RagflowIntegrationPaths;
}

export interface CallbackEndpointConfig {
  binding: CallbackBinding;
  tenantId: string | null;
  sourceSystem: string;
  baseUrl: string;
  path: string;
  method: string;
  enabled: boolean;
  credentialConfigured: boolean;
}

export interface SystemIntegrations {
  ragflow: RagflowIntegration;
  callbacksEnabled: boolean;
  callbacks: CallbackEndpointConfig[];
}

export type EamProbeStatus = 'connected' | 'failed';

export interface EamProbeResult {
  binding: string;
  probeUrl: string;
  status: EamProbeStatus;
  httpStatus: number | null;
  latencyMs: number | null;
  checkedAt: string;
  /** 后端线格式：仅失败时携带该键（成功时键不存在），故为可选。 */
  errorCode?: string | null;
}

export interface ConversationMetadataItem {
  conversationId: string;
  businessUserId: string;
  equipmentId: string | null;
  fixedAssetNo: string | null;
  status: string;
  ragflowChatId: string | null;
  ragflowSessionId: string | null;
  contextVersion: number;
  createdAt: string;
  lastMessageAt: string | null;
}

export interface ConversationMetadataPage {
  items: ConversationMetadataItem[];
  hasMore: boolean;
}

/** Server-side sort order; backend defaults to updatedAt/lastMessageAt desc. */
export type MetadataSortOrder = 'asc' | 'desc';

export type ConversationMetadataOrderBy =
  | 'conversationId'
  | 'businessUserId'
  | 'equipmentId'
  | 'fixedAssetNo'
  | 'status'
  | 'contextVersion'
  | 'createdAt'
  | 'lastMessageAt';

export interface DocumentMetadataItem {
  externalDocumentId: string;
  sourceVersionId: string;
  fileName: string;
  sourceSystem: string;
  documentType: string | null;
  equipmentId: string | null;
  fixedAssetNo: string | null;
  assetId: string | null;
  syncStatus: string | null;
  businessStatus: string;
  ragflowDatasetId: string | null;
  ragflowDocumentId: string | null;
  sourceSize: number | null;
  createdAt: string;
  updatedAt: string | null;
  /** RAGFlow 解析完成时间。 */
  parsedAt: string | null;
  /** 通知 EAM 的时间。 */
  eamNotifiedAt: string | null;
}

export interface DocumentMetadataPage {
  items: DocumentMetadataItem[];
  hasMore: boolean;
}

export type DocumentMetadataOrderBy =
  | 'externalDocumentId'
  | 'fileName'
  | 'sourceSystem'
  | 'documentType'
  | 'equipmentId'
  | 'fixedAssetNo'
  | 'assetId'
  | 'syncStatus'
  | 'businessStatus'
  | 'sourceSize'
  | 'createdAt'
  | 'updatedAt'
  | 'parsedAt'
  | 'eamNotifiedAt';

export interface MetadataSummary {
  conversations: {
    total: number;
    byStatus: Record<string, number>;
  };
  documents: {
    total: number;
    bySyncStatus: Record<string, number>;
    byBusinessStatus: Record<string, number>;
  };
}

export interface AdminConversationMessage {
  messageId: string;
  role: 'user' | 'assistant';
  content: string;
  /** 持久化的业务状态，原样展示，不按 citations 推导。 */
  status: string;
  createdAt: string;
}

export interface AdminConversationMessagesPage {
  conversationId: string;
  items: AdminConversationMessage[];
}

export interface RagDiagnosticEvent {
  type: string;
  atMs: number;
  data: Record<string, unknown>;
}

export interface RagDiagnostics {
  version: number;
  runId: string;
  startedAt: string;
  durationMs: number;
  events: RagDiagnosticEvent[];
  truncated: boolean;
}

export interface RagDiagnosticTraceItem {
  runId: string;
  conversationId: string;
  clientMessageId: string;
  status: string;
  outcome: string | null;
  startedAt: string | null;
  durationMs: number | null;
  truncated: boolean;
  createdAt: string;
}

export interface RagDiagnosticTracePage {
  items: RagDiagnosticTraceItem[];
  hasMore: boolean;
}

export interface RagDiagnosticTraceDetail {
  runId: string;
  conversationId: string;
  clientMessageId: string;
  status: string;
  createdAt: string;
  diagnostics: RagDiagnostics;
}
