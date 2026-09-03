import { useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { ChevronDown, ChevronRight, Globe2 } from 'lucide-react';
import { QuestionInput } from '../chat/QuestionInput';
import { ConsoleOverlay } from '../console/ConsoleOverlay';
import emptyChatIllustration from '../../assets/harness-empty-chat.png';
import type {
  Citation,
  ConversationDetail,
  DisplayError,
  HarnessMessage,
  ReasoningMode,
} from '../../api/v2Types';

interface HarnessChatProps {
  conversation: ConversationDetail | null;
  messages: HarnessMessage[];
  isStreaming: boolean;
  error: DisplayError | null;
  onSend: (question: string) => void;
  onRetry: () => void;
  onCancel: () => void;
  onCitation: (citation: Citation) => void;
  onCitationGroup?: (citations: Citation[]) => void;
  selectedFiles?: File[];
  onFilesPicked?: (files: File[]) => void;
  onRemoveFile?: (index: number) => void;
  reasoningMode: ReasoningMode;
  onReasoningModeChange: (mode: ReasoningMode) => void;
  internetEnabled: boolean;
  onInternetEnabledChange: (enabled: boolean) => void;
}

interface ReasoningOption {
  value: ReasoningMode;
  level: number;
  label: string;
  description: string;
}

export const REASONING_OPTIONS: ReasoningOption[] = [
  { value: 'simple', level: 0, label: '快速', description: '单轮直答，响应最快、消耗最少；适合明确的简单问题。' },
  { value: 'low', level: 1, label: '轻量', description: '做少量推理与校验，速度和准确度较均衡。' },
  { value: 'medium', level: 2, label: '标准', description: '增加问题拆解和依据核对，复杂问题更稳但响应更慢。' },
  { value: 'high', level: 3, label: '深度', description: '进行更深的多步推理，适合含糊或需要比较的问题，耗时和 token 更多。' },
  { value: 'ultra', level: 4, label: '极致', description: '最充分的推理与复核，准确度优先；速度最慢、成本最高。' },
];

const CITATION_HREF_PREFIX = '#harness-citation-';

function statusLabel(status: string): string {
  if (status === 'streaming') return '思考中';
  if (status === '已完成' || status === 'completed') return '业务状态：已完成';
  if (status === '无可靠依据' || status === 'no_reliable_evidence') return '业务状态：无可靠依据';
  if (status === '失败' || status === 'failed') return '业务状态：失败';
  return `业务状态：${status}`;
}

function citationForMarker(citations: Citation[], marker: number): Citation | undefined {
  return citations.find((citation) => citation.refIndex === marker)
    ?? citations[marker - 1];
}

function citationMarkdown(content: string): string {
  return content.replace(/\[(?:ID:)\s*(\d+)\]|\[(\d+)\]/gi, (_match, prefixed, plain) => {
    const marker = prefixed ?? plain;
    return `[${marker}](${CITATION_HREF_PREFIX}${marker})`;
  });
}

function CitationMarkdown({
  content,
  citations,
  onCitation,
}: {
  content: string;
  citations: Citation[];
  onCitation: (citation: Citation) => void;
}) {
  return (
    <div className="harness-markdown">
      <ReactMarkdown
        components={{
          a: ({ href, children, ...props }) => {
            const marker = href?.startsWith(CITATION_HREF_PREFIX)
              ? Number(href.slice(CITATION_HREF_PREFIX.length))
              : Number.NaN;
            if (Number.isInteger(marker) && marker > 0) {
              const citation = citationForMarker(citations, marker);
              return citation ? (
                <sup className="harness-citation-marker">
                  <button
                    type="button"
                    onClick={() => onCitation(citation)}
                    aria-label={`打开引用 ${marker}`}
                  >
                    {marker}
                  </button>
                </sup>
              ) : <sup className="harness-citation-marker">{marker}</sup>;
            }
            return (
              <a href={href} {...props} target="_blank" rel="noreferrer">
                {children}
              </a>
            );
          },
        }}
      >
        {citationMarkdown(content)}
      </ReactMarkdown>
    </div>
  );
}

interface CitationSourceGroup {
  citation: Citation;
  citations: Citation[];
  count: number;
  hasFigure: boolean;
}

function citationSourceKey(citation: Citation): string {
  if (citation.externalDocumentId) {
    return `document:${citation.externalDocumentId}:${citation.sourceVersionId ?? ''}`;
  }
  if (citation.recordId) return `record:${citation.recordType ?? ''}:${citation.recordId}`;
  if (citation.url) return `web:${citation.url}`;
  return `${citation.sourceType}:${citation.title}:${citation.fileKind ?? ''}`;
}

function groupCitationSources(citations: Citation[]): CitationSourceGroup[] {
  const groups = new Map<string, CitationSourceGroup>();
  citations.forEach((citation) => {
    const key = citationSourceKey(citation);
    const current = groups.get(key);
    const hasFigure = Boolean(citation.sourceType === 'document' && citation.fileKind === 'crop' && citation.downloadUrl);
    if (current) {
      current.count += 1;
      current.citations.push(citation);
      current.hasFigure ||= hasFigure;
    } else {
      groups.set(key, { citation, citations: [citation], count: 1, hasFigure });
    }
  });
  return [...groups.values()];
}

function isFailedStatus(status: string): boolean {
  return status === '失败' || status === 'failed';
}

export function HarnessChat({
  conversation,
  messages,
  isStreaming,
  error,
  onSend,
  onRetry,
  onCancel,
  onCitation,
  onCitationGroup,
  selectedFiles,
  onFilesPicked,
  onRemoveFile,
  reasoningMode,
  onReasoningModeChange,
  internetEnabled,
  onInternetEnabledChange,
}: HarnessChatProps) {
  const [selected, setSelected] = useState<string | null>(null);
  const [expandedReasoning, setExpandedReasoning] = useState<string | null>(null);
  const [reasoningOpen, setReasoningOpen] = useState(false);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);
  const reasoningTriggerRef = useRef<HTMLButtonElement | null>(null);
  const selectedReasoning = useMemo(
    () => REASONING_OPTIONS.find((option) => option.value === reasoningMode) ?? REASONING_OPTIONS[0],
    [reasoningMode],
  );

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView?.({ block: 'end' });
  }, [conversation?.conversationId, isStreaming, messages.length, messages[messages.length - 1]?.content.length]);

  const composerTools = (
    <div className="harness-composer-tools">
      <label className="harness-toggle">
        <input
          type="checkbox"
          aria-label="联网检索"
          checked={internetEnabled}
          disabled={!conversation || isStreaming}
          onChange={(event) => onInternetEnabledChange(event.target.checked)}
        />
        <span className="harness-toggle-track" aria-hidden="true">
          <span className="harness-toggle-thumb"><Globe2 size={12} /></span>
        </span>
      </label>
      <label className="harness-reasoning-field">
        <span className="sr-only">推理强度</span>
        <button
          ref={reasoningTriggerRef}
          type="button"
          className="harness-reasoning-trigger"
          aria-haspopup="dialog"
          aria-expanded={Boolean(reasoningOpen)}
          disabled={!conversation || isStreaming}
          onClick={() => setReasoningOpen((current) => !current)}
        >
          <span>{selectedReasoning.label}</span>
          <ChevronDown size={14} aria-hidden="true" />
        </button>
        <select
          aria-label="推理档位"
          className="sr-only"
          value={reasoningMode}
          disabled={!conversation || isStreaming}
          title={selectedReasoning.description}
          onChange={(event) => onReasoningModeChange(event.target.value as ReasoningMode)}
        >
          {REASONING_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      </label>
      <span data-testid="reasoning-mode-hint" className="sr-only">
        {selectedReasoning.description} {internetEnabled ? '开启联网后可能获得外部网页依据，但会增加检索耗时。' : '联网关闭，仅使用当前知识库与会话附件。'}
      </span>
    </div>
  );

  return (
    <section aria-label="问答会话" className="console-card harness-chat">
      <div className="console-card-body">
        <div className="harness-transcript">
          {!conversation && <p className="console-empty">先创建或选择会话。</p>}
          {conversation && messages.length === 0 && (
            <div className="harness-empty-state">
              <img src={emptyChatIllustration} alt="开始新的对话" />
              <h3>开始新的对话</h3>
              <p>输入你的问题，我将为你提供帮助</p>
            </div>
          )}
          {messages.map((message) => {
            if (message.role === 'user') {
              return (
                <div key={message.id} className="harness-bubble-user">
                  {message.content}
                  {message.attachments && message.attachments.length > 0 && (
                    <div className="harness-file-chips">
                      {message.attachments.map((item, index) => (
                        <span key={`${item.fileName}-${index}`} className="harness-file-chip">{item.fileName}</span>
                      ))}
                    </div>
                  )}
                  <div className="console-route">clientMessageId: {message.clientMessageId}</div>
                </div>
              );
            }
            const isFailed = isFailedStatus(message.status);
            const isThinking = message.thinking || (message.status === 'streaming' && !message.content);
            const hasReasoning = Boolean(message.reasoning?.trim());
            const sources = groupCitationSources(message.citations);
            return (
              <article
                key={message.id}
                data-message-status={message.status}
                className="harness-bubble-assistant"
              >
                <div className="harness-bubble-meta">
                  <span>{statusLabel(message.status)}</span>
                  <span>引用 {message.citations.length} 条</span>
                </div>
                {(isThinking || hasReasoning) && (
                  <div className={`harness-reasoning${expandedReasoning === message.id ? ' is-expanded' : ''}`}>
                    <button
                      type="button"
                      className="harness-reasoning-toggle"
                      aria-expanded={expandedReasoning === message.id}
                      onClick={() => setExpandedReasoning((current) => current === message.id ? null : message.id)}
                    >
                      <ChevronRight size={15} aria-hidden="true" className="harness-reasoning-chevron" />
                      {isThinking && <span className="console-thinking-dot" aria-hidden="true" />}
                      <span>{isThinking ? '思考中' : '思考过程'}</span>
                      {hasReasoning && <small>{expandedReasoning === message.id ? '收起' : '展开'}</small>}
                    </button>
                    {expandedReasoning === message.id && hasReasoning && (
                      <div className="harness-reasoning-content" aria-label="思考过程">
                        {message.reasoning}
                      </div>
                    )}
                    {isThinking && !hasReasoning && (
                      <div className="console-thinking-block"><span className="console-thinking-dot" aria-hidden="true" />思考中，正在生成回答…</div>
                    )}
                  </div>
                )}
                {message.content && (
                  <CitationMarkdown
                    content={message.content}
                    citations={message.citations}
                    onCitation={(citation) => {
                      setSelected(citation.citationId);
                      onCitation(citation);
                    }}
                  />
                )}
                {message.replayed && <p className="diag-help">本次请求为 server replay（clientMessageId 相同）。</p>}
                {sources.length > 0 && (
                  <div className="harness-source-list">
                    <div className="harness-source-list-label">唯一来源</div>
                    <div className="console-chip-row">
                      {sources.map((source) => (
                        <button
                          type="button"
                          key={citationSourceKey(source.citation)}
                          onClick={() => {
                            setSelected(source.citation.citationId);
                            if (onCitationGroup) onCitationGroup(source.citations);
                            else onCitation(source.citation);
                          }}
                          className="console-secondary-button harness-source-button"
                          aria-label={`查看来源 ${source.citation.title}（${source.count} 个片段）`}
                        >
                          <span>{source.citation.title}</span>
                          <small>
                            {source.count} 个片段
                            {source.hasFigure ? ' · 含引用图' : ''}
                            {source.citation.assetId ? ` · assetId: ${source.citation.assetId}` : ''}
                          </small>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                {message.citationError && <p className="diag-help">证据数据独立加载失败：{message.citationError}</p>}
                {message.error && <p className="console-alert">[{message.error.code}] {message.error.message}</p>}
                {isFailed && <button type="button" onClick={onRetry} disabled={isStreaming} className="console-secondary-button">使用相同 clientMessageId 重试</button>}
                {selected && selected === message.citations[0]?.citationId && <span className="sr-only">已选择 citation</span>}
              </article>
            );
          })}
          <div ref={transcriptEndRef} aria-hidden="true" />
        </div>
        {error && <p className="console-alert">[{error.code}] {error.message}</p>}
        <QuestionInput
          onSend={onSend}
          onCancel={onCancel}
          isStreaming={isStreaming}
          disabled={!conversation}
          selectedFiles={selectedFiles}
          onFilesPicked={onFilesPicked}
          onRemoveFile={onRemoveFile}
          variant="harness"
          composerTools={composerTools}
          resetKey={conversation?.conversationId ?? 'none'}
        />
        <ConsoleOverlay
          open={reasoningOpen}
          mode="popover"
          anchorRef={reasoningTriggerRef}
          onClose={() => setReasoningOpen(false)}
          ariaLabel="推理档位"
          className="harness-reasoning-popover"
        >
          <div className="harness-reasoning-popover-head">
            <div>
              <strong>推理档位</strong>
              <span>{selectedReasoning.label}</span>
            </div>
            <button type="button" className="console-icon-button" aria-label="关闭推理档位" onClick={() => setReasoningOpen(false)}>×</button>
          </div>
          <input
            className="harness-reasoning-range"
            type="range"
            min={0}
            max={REASONING_OPTIONS.length - 1}
            step={1}
            value={selectedReasoning.level}
            aria-label="推理强度滑块"
            aria-valuetext={selectedReasoning.label}
            disabled={!conversation || isStreaming}
            onChange={(event) => {
              const option = REASONING_OPTIONS[Number(event.target.value)];
              if (option) onReasoningModeChange(option.value);
            }}
          />
          <div className="harness-reasoning-range-labels" aria-hidden="true">
            {REASONING_OPTIONS.map((option) => <span key={option.value}>{option.label}</span>)}
          </div>
          <p className="harness-reasoning-description">{selectedReasoning.description}</p>
        </ConsoleOverlay>
      </div>
    </section>
  );
}
