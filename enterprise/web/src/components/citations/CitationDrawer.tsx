import { useEffect, useState } from 'react';
import { X, FileText, Database, MapPin, Calendar, Tag } from 'lucide-react';
import { api } from '../../api/client';
import type { Citation } from '../../api/types';

interface CitationDrawerProps {
  citationId: string | null;
  onClose: () => void;
}

export function CitationDrawer({ citationId, onClose }: CitationDrawerProps) {
  const [citation, setCitation] = useState<Citation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!citationId) {
      setCitation(null);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);

    api
      .getCitation(citationId)
      .then((cit) => {
        if (!cancelled) setCitation(cit);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message ?? '加载引用失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [citationId]);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100">
        <h3 className="text-sm font-semibold text-gray-700">引用详情</h3>
        <button
          onClick={onClose}
          className="p-1 rounded-md text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors"
          aria-label="关闭引用抽屉"
        >
          <X size={14} />
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4">
        {!citationId && (
          <p className="text-xs text-gray-400 text-center mt-8">
            点击回答中的引用编号查看详情
          </p>
        )}

        {loading && (
          <p className="text-xs text-gray-400 text-center mt-8">加载中...</p>
        )}

        {error && (
          <div className="p-3 rounded-md bg-red-50 border border-red-100">
            <p className="text-xs text-red-600">{error}</p>
          </div>
        )}

        {citation && (
          <div className="space-y-3">
            {/* Source type badge */}
            <div className="flex items-center gap-2">
              <span
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${
                  citation.sourceType === 'document'
                    ? 'bg-blue-50 text-blue-700'
                    : 'bg-green-50 text-green-700'
                }`}
              >
                {citation.sourceType === 'document' ? (
                  <FileText size={12} />
                ) : (
                  <Database size={12} />
                )}
                {citation.sourceType === 'document' ? '文档来源' : '业务记录'}
              </span>
            </div>

            {/* Title */}
            <h4 className="text-sm font-medium text-gray-800">{citation.title}</h4>

            {/* Details grid */}
            <div className="grid grid-cols-2 gap-2 text-xs">
              {citation.documentId && (
                <div className="flex items-center gap-1 text-gray-500">
                  <FileText size={12} />
                  <span className="truncate">文档: {citation.documentId}</span>
                </div>
              )}
              {citation.versionId && (
                <div className="flex items-center gap-1 text-gray-500">
                  <Tag size={12} />
                  <span className="truncate">版本: {citation.versionId}</span>
                </div>
              )}
              {citation.pageNo && (
                <div className="flex items-center gap-1 text-gray-500">
                  <MapPin size={12} />
                  <span>页码: {citation.pageNo}</span>
                </div>
              )}
              {citation.recordType && (
                <div className="flex items-center gap-1 text-gray-500">
                  <Calendar size={12} />
                  <span>类型: {citation.recordType}</span>
                </div>
              )}
              {citation.recordId && (
                <div className="flex items-center gap-1 text-gray-500 col-span-2">
                  <Tag size={12} />
                  <span className="truncate">记录: {citation.recordId}</span>
                </div>
              )}
            </div>

            {/* Excerpt */}
            {citation.excerpt && (
              <div className="p-3 rounded-md bg-gray-50 border border-gray-100">
                <p className="text-xs text-gray-600 leading-relaxed italic">
                  &ldquo;{citation.excerpt}&rdquo;
                </p>
              </div>
            )}

            {/* Page location hint */}
            {citation.bbox && (
              <div className="flex items-center gap-1 text-xs text-gray-400">
                <MapPin size={10} />
                <span>高亮区域: 第 {citation.pageNo} 页</span>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
