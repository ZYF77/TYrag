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
  if (status === 'completed') return '业务状态：completed';
  if (status === 'no_reliable_evidence') return '业务状态：no_reliable_evidence';
  if (status === 'failed') return '业务状态：failed';
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
    <section aria-label="SSE 问答验证" className="flex min-h-[520px] flex-col rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="border-b border-slate-100 px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold text-slate-800">真 SSE 问答与 citation</h2>
            <p className="mt-1 text-[11px] text-slate-500">POST /conversations/{conversation?.conversationId ?? '…'}/messages · Accept: text/event-stream</p>
          </div>
          {conversation && <span className="rounded-full bg-indigo-50 px-2 py-1 text-[11px] text-indigo-700">{conversation.title}</span>}
        </div>
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {!conversation && <p className="py-16 text-center text-xs text-slate-400">先创建或选择会话。</p>}
        {conversation && messages.length === 0 && <p className="py-16 text-center text-xs text-slate-400">发送问题以验证 v2 SSE。</p>}
        {messages.map((message) => {
          if (message.role === 'user') {
            return <div key={message.id} className="ml-auto max-w-[82%] rounded-xl bg-indigo-600 px-3 py-2 text-sm text-white">{message.content}<div className="mt-1 text-[10px] text-indigo-100">clientMessageId: {message.clientMessageId}</div></div>;
          }
          const isFailed = message.status === 'failed';
          return (
            <article
              key={message.id}
              data-message-status={message.status}
              className="rounded-xl border border-slate-100 bg-slate-50 p-3"
            >
              <div className="flex items-center justify-between gap-2 text-[11px] text-slate-500">
                <span>{statusLabel(message.status)}</span>
                <span>citations: {message.citations.length}</span>
              </div>
              {message.content && <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-800">{message.content}</p>}
              {message.status === 'streaming' && !message.content && <span className="mt-2 inline-block h-4 w-1.5 animate-pulse rounded bg-indigo-500" />}
              {message.replayed && <p className="mt-2 text-[11px] text-indigo-700">本次请求为 server replay（clientMessageId 相同）。</p>}
              <div className="mt-3 flex flex-wrap gap-2">
                {message.citations.map((citation, index) => (
                  <button
                    type="button"
                    key={citation.citationId}
                    onClick={() => {
                      setSelected(citation.citationId);
                      onCitation(citation);
                    }}
                    className={`rounded-md border px-2 py-1 text-xs ${citation.sourceType === 'document' ? 'border-blue-200 bg-blue-50 text-blue-700' : 'border-emerald-200 bg-emerald-50 text-emerald-700'}`}
                  >
                    [{index + 1}] {citation.title}
                    {citation.assetId ? ` · assetId: ${citation.assetId}` : ''}
                  </button>
                ))}
              </div>
              {message.citationError && <p className="mt-2 text-xs text-amber-700">证据数据独立加载失败：{message.citationError}</p>}
              {message.error && <p className="mt-2 text-xs text-rose-700">[{message.error.code}] {message.error.message}</p>}
              {isFailed && <button type="button" onClick={onRetry} disabled={isStreaming} className="mt-3 rounded-md border border-rose-200 bg-white px-2.5 py-1.5 text-xs font-medium text-rose-700 hover:bg-rose-50 disabled:opacity-50">使用相同 clientMessageId 重试</button>}
              {selected && selected === message.citations[0]?.citationId && <span className="sr-only">已选择 citation</span>}
            </article>
          );
        })}
      </div>
      {error && <p className="border-t border-slate-100 px-4 py-2 text-xs text-rose-700">[{error.code}] {error.message}</p>}
      <QuestionInput
        onSend={onSend}
        onCancel={onCancel}
        isStreaming={isStreaming}
        disabled={!conversation}
      />
    </section>
  );
}
