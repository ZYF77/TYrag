import { useState } from 'react';
import { QuestionInput } from '../chat/QuestionInput';
import type { Citation, ConversationDetail, DisplayError, HarnessMessage } from '../../api/v2Types';

interface HarnessChatProps {
  conversation: ConversationDetail | null;
  messages: HarnessMessage[];
  isStreaming: boolean;
  error: DisplayError | null;
  onSend: (question: string) => void;
  onRetry: () => void;
  onCancel: () => void;
  onCitation: (citation: Citation) => void;
}

function statusLabel(status: string): string {
  if (status === 'streaming') return '正在回答…';
  if (status === '已完成' || status === 'completed') return '业务状态：已完成';
  if (status === '无可靠依据' || status === 'no_reliable_evidence') return '业务状态：无可靠依据';
  if (status === '失败' || status === 'failed') return '业务状态：失败';
  return `业务状态：${status}`;
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
}: HarnessChatProps) {
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <section aria-label="SSE 问答验证" className="console-card harness-chat">
      <div className="console-card-head">
        <div>
          <p className="console-eyebrow">SSE</p>
          <h2>真 SSE 问答与 citation</h2>
          <p>POST /conversations/{conversation?.conversationId ?? '…'}/messages · Accept: text/event-stream</p>
        </div>
        {conversation && <span className="console-mode-badge">{conversation.title}</span>}
      </div>
      <div className="console-card-body">
        <div className="harness-transcript">
          {!conversation && <p className="console-empty">先创建或选择会话。</p>}
          {conversation && messages.length === 0 && <p className="console-empty">发送问题以验证 v2 SSE。</p>}
          {messages.map((message) => {
            if (message.role === 'user') {
              return (
                <div key={message.id} className="harness-bubble-user">
                  {message.content}
                  <div className="console-route">clientMessageId: {message.clientMessageId}</div>
                </div>
              );
            }
            const isFailed = message.status === '失败';
            return (
              <article
                key={message.id}
                data-message-status={message.status}
                className="harness-bubble-assistant"
              >
                <div className="harness-bubble-meta">
                  <span>{statusLabel(message.status)}</span>
                  <span>citations: {message.citations.length}</span>
                </div>
                {message.content && <p>{message.content}</p>}
                {message.status === 'streaming' && !message.content && <span className="console-status-dot console-status--processing" />}
                {message.replayed && <p className="diag-help">本次请求为 server replay（clientMessageId 相同）。</p>}
                <div className="console-chip-row">
                  {message.citations.map((citation, index) => (
                    <button
                      type="button"
                      key={citation.citationId}
                      onClick={() => {
                        setSelected(citation.citationId);
                        onCitation(citation);
                      }}
                      className="console-secondary-button"
                    >
                      [{index + 1}] {citation.title}
                      {citation.assetId ? ` · assetId: ${citation.assetId}` : ''}
                    </button>
                  ))}
                </div>
                {message.citationError && <p className="diag-help">证据数据独立加载失败：{message.citationError}</p>}
                {message.error && <p className="console-alert">[{message.error.code}] {message.error.message}</p>}
                {isFailed && <button type="button" onClick={onRetry} disabled={isStreaming} className="console-secondary-button">使用相同 clientMessageId 重试</button>}
                {selected && selected === message.citations[0]?.citationId && <span className="sr-only">已选择 citation</span>}
              </article>
            );
          })}
        </div>
        {error && <p className="console-alert">[{error.code}] {error.message}</p>}
        <QuestionInput
          onSend={onSend}
          onCancel={onCancel}
          isStreaming={isStreaming}
          disabled={!conversation}
        />
      </div>
    </section>
  );
}
