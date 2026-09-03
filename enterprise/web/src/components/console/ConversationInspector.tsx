import { useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { RefreshCw, X } from 'lucide-react';
import { toDisplayError, v2Api } from '../../api/v2Client';
import type { AdminConversationMessage, ConversationMetadataItem } from '../../api/consoleTypes';
import type { Citation, DisplayError } from '../../api/v2Types';
import { ConsoleOverlay } from './ConsoleOverlay';

const STATUS_PILL_TONES: Record<string, string> = {
  completed: 'ok',
  '已完成': 'ok',
  no_reliable_evidence: 'warn',
  '无可靠依据': 'warn',
  failed: 'failed',
  '失败': 'failed',
  running: 'processing',
  processing: 'processing',
  streaming: 'processing',
};

function formatTime(value: string | null | undefined): string {
  if (!value) return '未提供';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

function StatusPill({ code, label }: { code: string | null | undefined; label?: string }) {
  const value = code ?? '';
  return (
    <span className={`console-status console-status--${STATUS_PILL_TONES[value] ?? 'muted'}`}>
      <span className="console-status-dot" aria-hidden="true" />
      {label ?? (value || '未提供')}
    </span>
  );
}

const CITATION_HREF_PREFIX = '#console-inspector-citation-';

function citationForMarker(citations: Citation[], marker: number): Citation | undefined {
  return citations.find((citation) => citation.refIndex === marker) ?? citations[marker - 1];
}

function citationMarkdown(content: string, messageId: string): string {
  return content.replace(/\[(?:ID:)\s*(\d+)\]|\[(\d+)\]/gi, (_match, prefixed, plain) => {
    const marker = prefixed ?? plain;
    return `[${marker}](${CITATION_HREF_PREFIX}${messageId}-${marker})`;
  });
}

function InspectorCitationMarkdown({ content, citations, messageId }: { content: string; citations: Citation[]; messageId: string }) {
  const prefix = `${CITATION_HREF_PREFIX}${messageId}-`;
  return (
    <ReactMarkdown
      components={{
        a: ({ href, children, ...props }) => {
          const marker = href?.startsWith(prefix) ? Number(href.slice(prefix.length)) : Number.NaN;
          const citation = Number.isInteger(marker) && marker > 0 ? citationForMarker(citations, marker) : undefined;
          if (citation) {
            return <sup className="console-citation-marker"><a href={`#console-inspector-citation-${messageId}-${marker}`} {...props} aria-label={`查看引用 ${marker}`}>{marker}</a></sup>;
          }
          return <a href={href} {...props} target="_blank" rel="noreferrer">{children}</a>;
        },
      }}
    >
      {citationMarkdown(content, messageId)}
    </ReactMarkdown>
  );
}

function InspectorCitationList({ messageId, citations }: { messageId: string; citations: Citation[] }) {
  if (citations.length === 0) return null;
  return (
    <div className="console-admin-citations">
      <div className="console-admin-citations-title">引用来源</div>
      <ol>
        {citations.map((citation, index) => {
          const marker = citation.refIndex ?? index + 1;
          return (
            <li key={citation.citationId} id={`console-inspector-citation-${messageId}-${marker}`}>
              <span>{marker}</span>
              <div>
                <strong>{citation.title || '未命名来源'}</strong>
                <small>{citation.sourceType}{citation.pageNo != null ? ` · 第 ${citation.pageNo} 页` : ''}{citation.fileKind === 'crop' ? ' · 引用图' : ''}</small>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function statusLabel(status: string): string {
  if (['running', 'processing', 'streaming'].includes(status)) return '思考中';
  if (status === 'completed') return '已完成';
  if (status === 'no_reliable_evidence') return '无可靠依据';
  if (status === 'failed') return '失败';
  return status;
}

export function ConversationInspector({ conversation, onClose }: { conversation: ConversationMetadataItem; onClose: () => void }) {
  const [messages, setMessages] = useState<AdminConversationMessage[]>([]);
  const [error, setError] = useState<DisplayError | null>(null);
  const [loading, setLoading] = useState(true);
  const endRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await v2Api.getAdminConversationMessages(conversation.conversationId);
      setMessages(page.items);
    } catch (reason) {
      setError(toDisplayError(reason));
    } finally {
      setLoading(false);
    }
  }, [conversation.conversationId]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!loading) window.requestAnimationFrame(() => endRef.current?.scrollIntoView?.({ block: 'end' }));
  }, [loading, messages.length]);

  return (
    <ConsoleOverlay open mode="dialog" onClose={onClose} ariaLabel="会话详情" className="console-conversation-inspector-overlay">
      <section className="console-detail-dialog console-conversation-inspector" data-testid="console-conversation-inspector">
        <header className="console-detail-dialog-head">
          <div>
            <p className="console-eyebrow">Conversation</p>
            <h2>会话详情</h2>
            <p className="console-route">{conversation.conversationId}</p>
          </div>
          <div className="console-detail-dialog-actions">
            <button type="button" className="console-icon-button" aria-label="刷新会话详情" onClick={() => void load()}><RefreshCw size={16} /></button>
            <button type="button" className="console-icon-button" aria-label="关闭会话详情" onClick={onClose}><X size={17} /></button>
          </div>
        </header>
        <div className="console-detail-dialog-body console-conversation-inspector-body">
          <div className="console-chip-row">
            <span className="console-chip">业务用户 · {conversation.businessUserId}</span>
            {conversation.equipmentId && <span className="console-chip">设备 · {conversation.equipmentId}</span>}
            <StatusPill code={conversation.status} />
          </div>
          {loading && <p className="console-empty">对话消息加载中…</p>}
          {error && <div className="console-alert" role="alert">[{error.code}] {error.message}</div>}
          {!loading && !error && (
            <div className="console-chat console-admin-chat" aria-label="会话消息">
              {messages.length === 0 && <p className="console-empty">该会话暂无持久化消息。</p>}
              {messages.map((message) => {
                const thinking = message.role === 'assistant' && ['running', 'processing', 'streaming'].includes(message.status);
                return (
                  <article key={message.messageId} className={`console-chat-bubble console-chat-bubble--${message.role === 'user' ? 'user' : 'assistant'}`}>
                    <div className="console-chat-meta">
                      <span>{message.role === 'user' ? '用户' : 'EAM 回复'}</span>
                      <span>{formatTime(message.createdAt)}</span>
                      <StatusPill code={message.status} label={statusLabel(message.status)} />
                      {thinking && <span className="console-thinking-indicator"><span aria-hidden="true" />思考中</span>}
                    </div>
                    {message.content && (message.role === 'user'
                      ? <p className="console-chat-text">{message.content}</p>
                      : <InspectorCitationMarkdown content={message.content} citations={message.citations ?? []} messageId={message.messageId} />)}
                    {thinking && !message.content && <div className="console-thinking-block"><span className="console-thinking-dot" aria-hidden="true" />思考中，正在生成回答…</div>}
                    <InspectorCitationList messageId={message.messageId} citations={message.citations ?? []} />
                  </article>
                );
              })}
              <div ref={endRef} aria-hidden="true" />
            </div>
          )}
        </div>
      </section>
    </ConsoleOverlay>
  );
}
