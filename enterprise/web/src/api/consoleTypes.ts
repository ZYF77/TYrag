import type { Citation, DisplayError } from './v2Types';

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

export interface ConsoleAuthSession {
  authenticated: boolean;
  username: string;
  tenantId: string;
  expiresAt: string;
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
  caller_username?: string | null;
  http_status: number | null;
  duration_ms?: number | null;
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

export interface RagflowProcessingConfig {
  maxConcurrentTasks: number;
  maxConcurrentChunkBuilders: number;
  executorWorkers: number;
}

export interface RuntimeWorkerSettings {
  enabled: boolean;
  pollSeconds: number;
}

export interface RuntimeCleanupSettings extends RuntimeWorkerSettings {
  ttlSeconds: number;
}

export interface RuntimeQualityReconcilerSettings extends RuntimeWorkerSettings {
  runningTimeoutSeconds: number;
}

export interface RuntimeLimitsSettings {
  fileShareMaxMiB: number;
  s3MaxMiB: number;
  transientAttachmentMaxMiB: number;
}

export interface RuntimeDiagnosticsSettings {
  enabled: boolean;
}

export interface GatewayRuntimeSettings {
  outbox: RuntimeWorkerSettings;
  statusReconciler: RuntimeWorkerSettings;
  transientAttachmentCleanup: RuntimeCleanupSettings;
  qualityEvaluation: RuntimeWorkerSettings;
  qualityReconciler: RuntimeQualityReconcilerSettings;
  callbackDelivery: RuntimeWorkerSettings;
  limits: RuntimeLimitsSettings;
  diagnostics: RuntimeDiagnosticsSettings;
}

export interface GatewayRuntimeSettingsState {
  settings: GatewayRuntimeSettings;
  source: 'database' | 'environment' | string;
  updatedAt: string | null;
  hotReload: boolean;
}

export interface RagflowIntegration {
  baseUrl: string;
  apiVersion: string;
  paths: RagflowIntegrationPaths;
  processing?: RagflowProcessingConfig;
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
  gatewayProcessing?: {
    outboxInFlight: number;
    qualityInFlight: number;
    callbackBatch: number;
    callbackConcurrent: number;
  };
  limits?: {
    fileShareMaxBytes: number;
    s3MaxBytes: number;
    transientAttachmentMaxBytes: number;
    transientAttachmentMaxFiles: number;
  };
  runtime?: GatewayRuntimeSettingsState;
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

/** 管理员会话元数据高级检索条件；空值表示不参与筛选。 */
export interface ConversationMetadataFilters {
  conversationId?: string | null;
  businessUserId?: string | null;
  equipmentId?: string | null;
  fixedAssetNo?: string | null;
  ragflowId?: string | null;
  contextVersion?: number | null;
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
  currentVersion?: number | null;
  fileName: string;
  sourceKind?: string | null;
  parserProfile?: string | null;
  parserProfileVersion?: string | null;
  parserApplicationStatus?: string | null;
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

/** 管理员文件元数据高级检索条件；空值表示不参与筛选。 */
export interface DocumentMetadataFilters {
  externalDocumentId?: string | null;
  sourceVersionId?: string | null;
  fileName?: string | null;
  equipmentId?: string | null;
  fixedAssetNo?: string | null;
  assetId?: string | null;
  ragflowDocumentId?: string | null;
}

export interface AdminChunk {
  id: string;
  documentId: string;
  content: string;
  imageId: string | null;
  docType: string | null;
  available: boolean | number | null;
  positions: unknown;
  importantKeywords: unknown;
}

export interface DocumentMetadataDetail {
  item: DocumentMetadataItem;
  metadata: {
    mediaType: string | null;
    sourcePageCount: number | null;
    departmentId: string | null;
    securityLevel: number | null;
    documentSubtype: string | null;
    sourceDocumentType: string | null;
    ingestState: string | null;
    sourceState: string | null;
    sourceStateReason: string | null;
    attemptCount: number | null;
    parseRetryCount: number | null;
    lastErrorCode: string | null;
    lastErrorRetryable: boolean;
    lastSyncAt: string | null;
    sourceUpdatedAt: string | null;
  };
  parser: {
    applicationStatus: string | null;
    profile: string | null;
    profileVersion: string | null;
    expected: unknown;
    configured: unknown;
    executed: unknown;
    ragflow: {
      run: string | null;
      chunkMethod: string | null;
      chunkCount: number | null;
      tokenCount: number | null;
      progress: number | null;
      parserConfig: unknown;
    } | null;
    errorCode: string | null;
  };
}

export interface ChunkPage {
  items: AdminChunk[];
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
  state: 'ready' | 'not_ready' | string;
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
  /** 管理员会话查看器使用的安全 citation 摘要；详情仍由 Harness 入口授权查看。 */
  citations?: Citation[];
  createdAt: string;
}

export interface AdminConversationMessagesPage {
  conversationId: string;
  items: AdminConversationMessage[];
}

export interface RagDiagnosticEvent {
  type: string;
  atMs: number;
  /** Duration of this event itself; atMs remains cumulative from trace start. */
  durationMs?: number | null;
  /** Native source-relative timestamp for events merged from RAGFlow. */
  sourceAtMs?: number | null;
  data: Record<string, unknown>;
}

export interface RagDiagnostics {
  version: number;
  runId: string;
  startedAt: string;
  durationMs: number;
  timing?: {
    atMs?: string;
    durationMs?: string;
  };
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
