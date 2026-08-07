import { AlertCircle, AlertTriangle, CheckCircle2 } from 'lucide-react';
import type { ChatMessage } from '../../api/types';
import { CitationBadge } from '../citations/CitationBadge';

interface MessageItemProps {
  message: ChatMessage;
  onCitationClick: (citationId: string) => void;
}

export function MessageItem({ message, onCitationClick }: MessageItemProps) {
  const isUser = message.role === 'user';

  if (isUser) {
    return (
      <div className="flex justify-end px-4 py-2">
        <div className="max-w-[75%] bg-blue-600 text-white text-sm rounded-2xl rounded-br-md px-4 py-2 shadow-sm">
          {message.content}
        </div>
      </div>
    );
  }

  // Assistant reply
  const reply = message;
  const statusIcon = () => {
    switch (reply.status) {
      case 'streaming':
        return <span className="inline-block w-2 h-2 bg-blue-500 rounded-full animate-pulse" />;
      case 'completed':
        return <CheckCircle2 size={14} className="text-green-500" />;
      case 'failed':
        return <AlertCircle size={14} className="text-red-500" />;
      case 'degraded':
        return <AlertTriangle size={14} className="text-yellow-500" />;
      case 'no_reliable_evidence':
        return <AlertCircle size={14} className="text-orange-500" />;
    }
  };

  return (
    <div className="px-4 py-3">
      <div className="flex items-start gap-2">
        <div className="w-6 h-6 rounded-full bg-gray-100 flex items-center justify-center flex-shrink-0 mt-0.5">
          <span className="text-[10px] font-bold text-gray-500">AI</span>
        </div>
        <div className="min-w-0 flex-1">
          {/* Status indicator */}
          <div className="flex items-center gap-1.5 mb-1">
            {statusIcon()}
            <span className="text-[10px] text-gray-400">
              {reply.status === 'streaming' && '正在回答...'}
              {reply.status === 'completed' && '回答完成'}
              {reply.status === 'failed' && '回答失败'}
              {reply.status === 'degraded' && '降级回答（部分服务不可用）'}
              {reply.status === 'no_reliable_evidence' && '未找到可靠证据'}
            </span>
          </div>

          {/* Content */}
          {reply.content && (
            <div className="text-sm text-gray-700 leading-relaxed whitespace-pre-wrap">
              {reply.content}
            </div>
          )}

          {/* Streaming cursor */}
          {reply.status === 'streaming' && !reply.content && (
            <span className="inline-block w-1.5 h-4 bg-blue-500 animate-pulse rounded-sm" />
          )}

          {/* Error */}
          {reply.error && (
            <div className="mt-2 p-2 rounded-md bg-red-50 border border-red-100">
              <p className="text-xs text-red-600">
                [{reply.error.code}] {reply.error.message}
              </p>
              {reply.error.requestId && (
                <p className="text-[10px] text-red-400 mt-1">
                  Request ID: {reply.error.requestId}
                </p>
              )}
            </div>
          )}

          {/* No evidence message */}
          {reply.status === 'no_reliable_evidence' && (
            <div className="mt-2 p-2 rounded-md bg-orange-50 border border-orange-100">
              <p className="text-xs text-orange-600">
                当前知识库中没有足够的证据来回答此问题。您可以尝试：
              </p>
              <ul className="mt-1 text-xs text-orange-500 list-disc list-inside">
                <li>更换问题措辞</li>
                <li>检查设备上下文是否正确</li>
                <li>联系知识管理员补充相关文档</li>
              </ul>
            </div>
          )}

          {/* Citations */}
          {reply.citations.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {reply.citations.map((cit, idx) => (
                <CitationBadge
                  key={cit.citationId}
                  citation={cit}
                  index={idx + 1}
                  onClick={() => onCitationClick(cit.citationId)}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
