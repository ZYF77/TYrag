// Types derived from contracts/integration-openapi.yaml (frozen v1.0.0)

export interface DocumentUpsertRequest {
  eventId: string;
  sourceSystem: string;
  externalDocumentId: string;
  sourceVersionId: string;
  sha256: string;
  fileName: string;
  mediaType?: string;
  source: {
    bucket: string;
    objectKey: string;
  };
  metadata: Record<string, unknown>;
}

export interface ErrorResponse {
  code: string;
  message: string;
  requestId: string;
  retryable?: boolean;
  details?: Record<string, unknown>;
}

export interface DocumentSyncResponse {
  externalDocumentId: string;
  sourceVersionId: string;
  ragflowDatasetId: string | null;
  ragflowDocumentId: string | null;
  status: string;
  stage: string | null;
  deduplicated: boolean;
  error: ErrorResponse | null;
}

export interface CreateConversationRequest {
  equipmentId?: string | null;
  fixedAssetNo?: string | null;
  faultCode?: string | null;
}

export interface Conversation {
  conversationId: string;
  ragflowSessionId: string;
  createdAt: string;
  // UI-only fields for the list
  title?: string;
  equipmentId?: string | null;
  fixedAssetNo?: string | null;
  faultCode?: string | null;
}

export interface DemoDocumentStatus {
  externalDocumentId: string;
  sourceVersionId: string;
  ragflowDatasetId: string | null;
  ragflowDocumentId: string | null;
  status: string;
  stage: string | null;
  deduplicated: boolean;
}

export interface DemoAskRequest {
  externalDocumentId: string;
  question: string;
  conversationId?: string | null;
}

export interface DemoAskResponse {
  answer: string;
  citations: Citation[];
  conversationId: string;
  ragflowSessionId: string | null;
  status: 'completed' | 'no_reliable_evidence' | 'failed';
}

export interface DemoConversationMessage {
  messageId: string;
  role: 'user' | 'assistant';
  content: string;
  citations: Citation[];
  status: string;
  createdAt: string;
}

export interface DemoConversation {
  conversationId: string;
  ragflowSessionId: string | null;
  messages: DemoConversationMessage[];
}

export interface UserPrincipal {
  businessUserId: string;
  displayName: string;
  tenantId: string;
  departmentIds: string[];
  roles: string[];
  capabilities: string[];
  securityLevel: number;
  mappingStatus: string;
}

export interface AskRequest {
  question: string;
  equipmentId?: string | null;
  faultCode?: string | null;
}

export type SourceType = 'document' | 'business_record';

export interface Citation {
  citationId: string;
  sourceType: SourceType;
  title: string;
  documentId: string | null;
  versionId: string | null;
  pageNo: number | null;
  bbox: { x1: number; y1: number; x2: number; y2: number } | null;
  assetId: string | null;
  excerpt: string | null;
  recordType: string | null;
  recordId: string | null;
}

// SSE Event types (per contracts/status-state-machine.md and docs/07)
export type SseEventType =
  | 'run.started'
  | 'retrieval.completed'
  | 'citation'
  | 'answer.delta'
  | 'answer.completed'
  | 'run.failed'
  | 'heartbeat';

export interface SseEvent {
  event: SseEventType;
  data: string;
}

export interface SseRunStartedData {
  runId: string;
}

export interface SseCitationData {
  citationId: string;
}

export interface SseAnswerDeltaData {
  content: string;
}

export interface SseAnswerCompletedData {
  runId: string;
  status?: string;
}

export interface SseRunFailedData {
  runId: string;
  error: ErrorResponse;
}

// Reply message model
export interface ReplyMessage {
  id: string;
  role: 'assistant';
  content: string;
  citations: Citation[];
  status:
    | 'streaming'
    | 'completed'
    | 'failed'
    | 'degraded'
    | 'no_reliable_evidence';
  error?: ErrorResponse;
  createdAt: string;
}

export interface UserMessage {
  id: string;
  role: 'user';
  content: string;
  createdAt: string;
}

export type ChatMessage = UserMessage | ReplyMessage;

// File sync status
export type SyncStatus =
  | 'received'
  | 'validated'
  | 'accepted'
  | 'transferring'
  | 'registering'
  | 'tracking'
  | 'registered'
  | 'queued'
  | 'uploaded'
  | 'waiting'
  | 'parsing'
  | 'indexing'
  | 'validating'
  | 'review_required'
  | 'ready'
  | 'failed'
  | 'cancelled'
  | 'superseded'
  | 'disabled'
  | 'deleted'
  | 'retry_wait'
  | 'completed';

export interface FileSyncItem {
  externalDocumentId: string;
  sourceVersionId?: string | null;
  fileName: string;
  status: SyncStatus;
  stage: string | null;
  error: ErrorResponse | null;
  updatedAt: string;
  businessStatus?: string;
  currentVersion?: boolean;
  batchId?: string | null;
}
