import { AlertCircle, CheckCircle2, FileText, RefreshCw } from 'lucide-react';
import type { DemoDocumentStatus, ErrorResponse } from '../../api/types';

interface DemoDocumentStatusCardProps {
  externalDocumentId: string;
  status: DemoDocumentStatus | null;
  loading: boolean;
  error: ErrorResponse | null;
  onRefresh: () => void;
}

function statusLabel(status: string | undefined): string {
  switch (status) {
    case 'ready':
      return '可查询';
    case 'parsing':
      return '解析中';
    case 'failed':
      return '失败';
    default:
      return status ?? '未配置';
  }
}

export function DemoDocumentStatusCard({
  externalDocumentId,
  status,
  loading,
  error,
  onRefresh,
}: DemoDocumentStatusCardProps) {
  const ready = status?.status === 'ready';

  return (
    <div className="px-4 py-3 border-b border-gray-100 bg-white">
      <div className="flex items-center gap-2">
        <FileText size={14} className="text-gray-400 flex-shrink-0" />
        <span className="text-xs font-medium text-gray-500">文档</span>
        <span className="text-xs font-medium text-gray-700 truncate">
          {externalDocumentId || '未配置'}
        </span>
        {loading ? (
          <span className="text-xs text-gray-400">查询中...</span>
        ) : status ? (
          <span
            className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${
              ready
                ? 'bg-green-50 text-green-700'
                : status.status === 'failed'
                  ? 'bg-red-50 text-red-700'
                  : 'bg-amber-50 text-amber-700'
            }`}
          >
            {ready ? <CheckCircle2 size={12} /> : <AlertCircle size={12} />}
            {statusLabel(status.status)}
          </span>
        ) : (
          <span className="text-xs text-gray-400">未查询</span>
        )}
        <button
          onClick={onRefresh}
          className="ml-auto p-1 rounded-md text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors"
          aria-label="刷新文档状态"
          title="刷新文档状态"
        >
          <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>
      {error && (
        <p className="mt-1 text-[10px] text-red-600">
          [{error.code}] {error.message}
        </p>
      )}
    </div>
  );
}
