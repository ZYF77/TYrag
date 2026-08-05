import { X, File, AlertTriangle, Check, Loader, Clock, Ban } from 'lucide-react';
import type { FileSyncItem } from '../../api/types';

interface FileSyncStatusProps {
  items: FileSyncItem[];
  loading: boolean;
  onClose: () => void;
  onRefresh: () => void;
}

function StatusIcon({ status }: { status: string }) {
  switch (status) {
    case 'ready':
      return <Check size={12} className="text-green-500" />;
    case 'parsing':
    case 'indexing':
    case 'queued':
    case 'registering':
    case 'transferring':
    case 'tracking':
    case 'validating':
    case 'validated':
    case 'waiting':
      return <Loader size={12} className="text-blue-500 animate-spin" />;
    case 'failed':
      return <AlertTriangle size={12} className="text-red-500" />;
    case 'uploaded':
      return <Clock size={12} className="text-yellow-500" />;
    case 'disabled':
      return <Ban size={12} className="text-gray-400" />;
    case 'deleted':
      return <Ban size={12} className="text-gray-400" />;
    case 'superseded':
      return <Check size={12} className="text-gray-400" />;
    case 'completed':
      return <Check size={12} className="text-green-500" />;
    case 'cancelled':
      return <X size={12} className="text-gray-400" />;
    case 'retry_wait':
      return <Clock size={12} className="text-yellow-500" />;
    case 'received':
    case 'accepted':
      return <Clock size={12} className="text-gray-400" />;
    case 'review_required':
      return <AlertTriangle size={12} className="text-orange-500" />;
    default:
      return <Clock size={12} className="text-gray-400" />;
  }
}

function StatusLabel({ status }: { status: string }) {
  const labels: Record<string, string> = {
    uploaded: '已上传',
    waiting: '等待处理',
    received: '已接收',
    validated: '已校验',
    accepted: '已受理',
    transferring: '传输中',
    registering: '注册中',
    tracking: '跟踪中',
    registered: '已注册',
    queued: '排队中',
    parsing: '解析中',
    indexing: '索引中',
    validating: '校验中',
    review_required: '待复核',
    ready: '可查询',
    failed: '失败',
    cancelled: '已取消',
    superseded: '已取代',
    disabled: '已停用',
    deleted: '已删除',
    retry_wait: '等待重试',
    completed: '已完成',
  };
  return <span>{labels[status] ?? status}</span>;
}

export function FileSyncStatus({
  items,
  loading,
  onClose,
}: FileSyncStatusProps) {
  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100">
        <h3 className="text-sm font-semibold text-gray-700">文件同步状态</h3>
        <button
          onClick={onClose}
          className="p-1 rounded-md text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors"
          aria-label="关闭文件同步面板"
        >
          <X size={14} />
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {loading && (
          <p className="text-xs text-gray-400 text-center mt-8">加载中...</p>
        )}

        {!loading && items.length === 0 && (
          <p className="text-xs text-gray-400 text-center mt-8">暂无同步记录</p>
        )}

        {items.map((item) => (
          <div
            key={item.externalDocumentId}
            className="px-4 py-2.5 border-b border-gray-50 hover:bg-gray-50 transition-colors"
          >
            <div className="flex items-start gap-2">
              <div className="mt-0.5">
                <StatusIcon status={item.status} />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <File size={12} className="text-gray-300 flex-shrink-0" />
                  <p className="text-xs font-medium text-gray-700 truncate">
                    {item.fileName}
                  </p>
                </div>
                <div className="flex items-center gap-2 mt-1">
                  <span className="text-[10px] text-gray-400">
                    <StatusLabel status={item.status} />
                  </span>
                  {item.stage && (
                    <span className="text-[10px] text-gray-300">
                      · {item.stage}
                    </span>
                  )}
                </div>
                {item.error && (
                  <p className="text-[10px] text-red-500 mt-1 truncate">
                    {item.error.message}
                  </p>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
