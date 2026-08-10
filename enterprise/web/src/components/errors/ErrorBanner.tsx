import { X, AlertTriangle, ShieldAlert, LogIn, WifiOff, FileQuestion } from 'lucide-react';
import { useState, useEffect } from 'react';

interface ErrorBannerProps {
  error: {
    code: string;
    message: string;
    requestId?: string;
    httpStatus?: number;
    retryable?: boolean;
  };
  onDismiss?: () => void;
}

function errorConfig(code: string): {
  icon: typeof AlertTriangle;
  bgColor: string;
  borderColor: string;
  textColor: string;
  title: string;
  suggestion: string;
} {
  switch (code) {
    case 'AUTH_TOKEN_INVALID':
    case 'AUTH_TOKEN_EXPIRED':
      return {
        icon: LogIn,
        bgColor: 'bg-amber-50',
        borderColor: 'border-amber-200',
        textColor: 'text-amber-800',
        title: '登录已过期',
        suggestion: '请重新登录后继续使用。您的会话凭证已失效。',
      };
    case 'AUTH_TOKEN_MISSING':
      return {
        icon: LogIn,
        bgColor: 'bg-amber-50',
        borderColor: 'border-amber-200',
        textColor: 'text-amber-800',
        title: '未登录',
        suggestion: '请配置有效的登录凭证后继续使用。',
      };
    case 'AUTH_USER_MAPPING_MISSING':
    case 'ACL_DENIED':
    case 'BUSINESS_QUERY_DENIED':
    case 'CONVERSATION_OWNER_MISMATCH':
      return {
        icon: ShieldAlert,
        bgColor: 'bg-red-50',
        borderColor: 'border-red-200',
        textColor: 'text-red-800',
        title: '权限不足',
        suggestion: '您没有权限访问此资源。如需访问请联系管理员。',
      };
    case 'DOCUMENT_NOT_READY':
      return {
        icon: AlertTriangle,
        bgColor: 'bg-amber-50',
        borderColor: 'border-amber-200',
        textColor: 'text-amber-800',
        title: '文档未就绪',
        suggestion: '文档仍在同步或解析中，请等待状态变为可查询后再提问。',
      };
    case 'CONVERSATION_CONTEXT_CONFLICT':
    case 'CONVERSATION_CONTEXT_INVALID':
    case 'CONVERSATION_CONTEXT_STALE':
    case 'CLIENT_MESSAGE_ID_CONFLICT':
    case 'SUGGESTION_STALE':
    case 'CONVERSATION_ARCHIVED':
      return {
        icon: AlertTriangle,
        bgColor: 'bg-amber-50',
        borderColor: 'border-amber-200',
        textColor: 'text-amber-800',
        title: '请求冲突（409）',
        suggestion: '当前会话或客户端请求与服务端状态不一致，请刷新状态后重试。',
      };
    case 'VALIDATION_ERROR':
    case 'DOCUMENT_METADATA_INVALID':
    case 'DOCUMENT_HASH_MISMATCH':
      return {
        icon: AlertTriangle,
        bgColor: 'bg-orange-50',
        borderColor: 'border-orange-200',
        textColor: 'text-orange-800',
        title: '请求参数无效（422）',
        suggestion: '请按 v2 契约检查字段、metadata snake_case 和输入格式。',
      };
    case 'CONVERSATION_NOT_FOUND':
      return {
        icon: FileQuestion,
        bgColor: 'bg-gray-50',
        borderColor: 'border-gray-200',
        textColor: 'text-gray-700',
        title: '会话不存在',
        suggestion: '该会话可能已被删除或您无权访问。请选择其他会话或新建。',
      };
    case 'RAGFLOW_UNAVAILABLE':
    case 'RAGFLOW_API_INCOMPATIBLE':
    case 'MODEL_TIMEOUT':
    case 'GATEWAY_UNAVAILABLE':
    case 'NETWORK_ERROR':
      return {
        icon: WifiOff,
        bgColor: 'bg-orange-50',
        borderColor: 'border-orange-200',
        textColor: 'text-orange-800',
        title: '服务暂时不可用',
        suggestion: '知识库服务当前响应异常，部分功能降级。请稍后重试。',
      };
    case 'RAGFLOW_SCOPE_VIOLATION':
      return {
        icon: ShieldAlert,
        bgColor: 'bg-red-50',
        borderColor: 'border-red-200',
        textColor: 'text-red-800',
        title: '检索范围异常',
        suggestion: '检索返回了无权访问的文档，本次回答已拦截，请联系管理员检查。',
      };
    case 'ASSET_REGISTRY_UNAVAILABLE':
    case 'AUTH_REPLAY_STORE_UNAVAILABLE':
    case 'RUN_INTERRUPTED':
      return {
        icon: WifiOff,
        bgColor: 'bg-orange-50',
        borderColor: 'border-orange-200',
        textColor: 'text-orange-800',
        title: '依赖服务暂时不可用（503）',
        suggestion: '服务端已保留可诊断的错误码；稍后可使用相同 clientMessageId 重试。',
      };
    default:
      return {
        icon: AlertTriangle,
        bgColor: 'bg-red-50',
        borderColor: 'border-red-200',
        textColor: 'text-red-700',
        title: '请求失败',
        suggestion: '遇到意外错误，请稍后重试。',
      };
  }
}

export function ErrorBanner({ error, onDismiss }: ErrorBannerProps) {
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    setDismissed(false);
  }, [error.code]);

  if (dismissed) return null;

  const config = errorConfig(error.code);
  const Icon = config.icon;

  const handleDismiss = () => {
    setDismissed(true);
    onDismiss?.();
  };

  return (
    <div
      role="alert"
      className={`mx-4 mt-3 px-4 py-3 rounded-lg border ${config.bgColor} ${config.borderColor} flex items-start gap-3`}
    >
      <Icon size={18} className={`${config.textColor} mt-0.5 flex-shrink-0`} />
      <div className="min-w-0 flex-1">
        <p className={`text-sm font-semibold ${config.textColor}`}>
          {config.title}
        </p>
        <p className={`text-xs ${config.textColor} mt-0.5 opacity-80`}>
          {error.message}
        </p>
        {error.httpStatus ? (
          <p className={`text-[10px] ${config.textColor} mt-1 opacity-70`}>
            HTTP {error.httpStatus}{error.retryable ? ' · retryable' : ''}
          </p>
        ) : null}
        <p className={`text-xs ${config.textColor} mt-1 opacity-70`}>
          {config.suggestion}
        </p>
        {error.requestId && (
          <p className="text-[10px] text-gray-400 mt-1">
            Request ID: {error.requestId}
          </p>
        )}
      </div>
      <button
        onClick={handleDismiss}
        className={`p-1 rounded-md ${config.textColor} opacity-50 hover:opacity-100 transition-opacity`}
        aria-label="关闭错误提示"
      >
        <X size={14} />
      </button>
    </div>
  );
}
