import { useState } from 'react';
import { KeyRound, RefreshCw, Save, X } from 'lucide-react';
import type { DemoDocumentStatus, ErrorResponse } from '../../api/types';

interface DemoSettingsPanelProps {
  externalDocumentId: string;
  tokenConfigured: boolean;
  status: DemoDocumentStatus | null;
  statusLoading: boolean;
  statusError: ErrorResponse | null;
  onSave: (externalDocumentId: string, token: string) => void;
  onRefreshStatus: () => void;
  onClose: () => void;
}

export function DemoSettingsPanel({
  externalDocumentId,
  tokenConfigured,
  status,
  statusLoading,
  statusError,
  onSave,
  onRefreshStatus,
  onClose,
}: DemoSettingsPanelProps) {
  const [docId, setDocId] = useState(externalDocumentId);
  const [token, setToken] = useState('');

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100">
        <h3 className="text-sm font-semibold text-gray-700">联调设置</h3>
        <button
          onClick={onClose}
          className="p-1 rounded-md text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors"
          aria-label="关闭联调设置"
        >
          <X size={14} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        <div>
          <label
            htmlFor="demo-document-id"
            className="block text-xs font-medium text-gray-500 mb-1"
          >
            externalDocumentId
          </label>
          <input
            id="demo-document-id"
            value={docId}
            onChange={(e) => setDocId(e.target.value)}
            placeholder="E2E-Doc1"
            className="w-full px-3 py-2 rounded-md border border-gray-200 text-sm text-gray-700 outline-none focus:border-blue-400 focus:ring-1 focus:ring-blue-100"
          />
          <button
            onClick={onRefreshStatus}
            className="mt-2 inline-flex items-center gap-1 px-2 py-1 rounded-md border border-gray-200 text-xs text-gray-600 hover:bg-gray-50 transition-colors"
          >
            <RefreshCw size={12} className={statusLoading ? 'animate-spin' : ''} />
            查询状态
          </button>
          {status && (
            <p className="mt-1 text-xs text-gray-500">
              {status.status} {status.stage ? `· ${status.stage}` : ''}
            </p>
          )}
          {statusError && (
            <p className="mt-1 text-[10px] text-red-600">
              [{statusError.code}] {statusError.message}
            </p>
          )}
        </div>

        <div>
          <label
            htmlFor="demo-jwt"
            className="block text-xs font-medium text-gray-500 mb-1"
          >
            JWT
          </label>
          <div className="flex items-center gap-2">
            <div className="relative flex-1">
              <KeyRound
                size={14}
                className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400"
              />
              <input
                id="demo-jwt"
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder={tokenConfigured ? '已配置，可留空' : '粘贴 JWT'}
                className="w-full pl-8 pr-3 py-2 rounded-md border border-gray-200 text-sm text-gray-700 outline-none focus:border-blue-400 focus:ring-1 focus:ring-blue-100"
              />
            </div>
            <button
              onClick={() => onSave(docId.trim(), token.trim())}
              className="inline-flex items-center gap-1 px-3 py-2 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 transition-colors"
            >
              <Save size={12} />
              保存
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
