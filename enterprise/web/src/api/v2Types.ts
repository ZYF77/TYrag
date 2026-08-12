export interface ErrorResponse {
  code: string;
  message: string;
  requestId: string;
  retryable: boolean;
  details?: Record<string, unknown>;
}

export interface DisplayError extends ErrorResponse {
  httpStatus?: number;
}

export interface SourceObject {
  bucket: string;
  objectKey: string;
}

export interface DocumentMetadata {
  schema_version: 1;
  tenant_id: string;
  source_system: string;
  external_document_id: string;
  equipment_id: string;
  fixed_asset_no?: string | null;
  asset_id?: string | null;
  document_type: string;
  document_version: string;
  department_id: string;
  security_level: number;
  allow_group_ids?: string[];
  deny_group_ids?: string[];
  business_status: 'active' | 'superseded' | 'disabled' | 'deleted' | 'review_required';
}

export interface DocumentCommand {
  eventId: string;
  eventType: 'upsert' | 'reindex';
  tenantId: string;
  sourceSystem: string;
  externalDocumentId: string;
  sourceVersionId: string;
  sha256: string;
  fileName: string;
  mediaType: string;
  source: SourceObject;
  metadata: DocumentMetadata;
  batchId?: string | null;
}

export interface DocumentOperation {
  operationId: string;
  externalDocumentId: string;
  sourceVersionId: string;
  status: string;
  stage: string;
  deduplicated: boolean;
  businessStatus: string;
  currentVersion: boolean;
  eventStatus: string;
  updatedAt: string;
}

export interface DocumentOperationPage {
  items: DocumentOperation[];
  nextCursor: string | null;
  hasMore: boolean;
}

export interface ConversationContext {
  equipmentId: string | null;
  fixedAssetNo: string | null;
  faultCode: string | null;
  contextVersion: number;
  registryVersion: string | null;
}

export interface CreateConversationRequest {
  equipmentId?: string | null;
  fixedAssetNo?: string | null;
  faultCode?: string | null;
}

export interface ConversationSummary {
  conversationId: string;
  title: string;
  status: 'active' | 'archived';
  equipmentId: string | null;
  fixedAssetNo: string | null;
  faultCode: string | null;
  contextVersion: number;
  lastMessageAt: string;
  createdAt: string;
}

export interface ConversationDetail extends ConversationSummary {
  context: ConversationContext;
}

export interface ConversationPage {
  items: ConversationSummary[];
  nextCursor: string | null;
  hasMore: boolean;
}

export interface PatchConversationContextRequest {
  equipmentId?: string | null;
  fixedAssetNo?: string | null;
  faultCode?: string | null;
}

export interface ConversationAttachmentRequest {
  fileName: string;
  mediaType: string;
  content: string;
}

export interface ConversationAttachmentResponse {
  attachmentId: string;
  conversationId: string;
  fileName: string;
  mediaType: string;
  sizeBytes: number;
  sha256: string;
  expiresAt: string;
  indexPolicy: 'never';
  maxDownloads: number;
  downloadCount: number;
  downloadUrl: string;
  ticketExpiresAt: string;
}

export type MessageStatus = 'completed' | 'no_reliable_evidence' | 'failed';

export interface Citation {
  citationId: string;
  sourceType: 'document' | 'business_record' | 'timeseries';
  title: string;
  externalDocumentId: string | null;
  sourceVersionId: string | null;
  pageNo?: number | null;
  bbox?: Record<string, unknown> | null;
  assetId?: string | null;
  excerpt?: string | null;
  recordType?: string | null;
  recordId?: string | null;
}

export interface Message {
  messageId: string;
  role: 'user' | 'assistant';
  content: string;
  status: MessageStatus;
  citations: Citation[];
  createdAt: string;
}

export interface MessagePage {
  items: Message[];
  nextCursor: string | null;
  hasMore: boolean;
}

export interface QuestionMessageRequest {
  clientMessageId: string;
  question: string;
}

export interface SuggestionMessageRequest {
  clientMessageId: string;
  suggestionId: string;
  contextVersion: number;
}

export type CreateMessageRequest =
  | QuestionMessageRequest
  | SuggestionMessageRequest;

export interface MessageRunResult {
  conversationId: string;
  clientMessageId: string;
  runId: string;
  messageId: string;
  answer: string;
  status: MessageStatus;
  citations: Citation[];
  replayed: boolean;
}

export interface MessageRunPending {
  conversationId: string;
  clientMessageId: string;
  runId: string;
  status: 'running';
  replayed: true;
}

export interface Suggestion {
  suggestionId: string;
  label: string;
  displayPrompt: string;
  contextVersion: number;
  expiresAt?: string | null;
}

export interface SuggestionPage {
  items: Suggestion[];
  contextVersion: number;
}

export type SseEventType =
  | 'run.started'
  | 'retrieval.completed'
  | 'citation'
  | 'answer.delta'
  | 'answer.completed'
  | 'run.failed'
  | 'heartbeat'
  | 'stream.end'
  | string;

export interface SseEvent {
  event: SseEventType;
  data: string;
}

export interface HarnessUserMessage {
  id: string;
  role: 'user';
  content: string;
  createdAt: string;
  clientMessageId: string;
}

export interface HarnessAssistantMessage {
  id: string;
  role: 'assistant';
  content: string;
  status: 'streaming' | MessageStatus;
  citations: Citation[];
  createdAt: string;
  clientMessageId: string;
  runId?: string;
  replayed?: boolean;
  error?: DisplayError;
  citationError?: string;
}

export type HarnessMessage = HarnessUserMessage | HarnessAssistantMessage;
