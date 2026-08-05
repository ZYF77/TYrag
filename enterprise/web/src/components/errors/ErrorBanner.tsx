import { X, AlertTriangle, ShieldAlert, LogIn, WifiOff, FileQuestion } from 'lucide-react';
import { useState, useEffect } from 'react';

interface ErrorBannerProps {
  error: {
    code: string;
    message: string;
    requestId?: string;
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
      return {
        icon: LogIn,
        bgColor: 'bg-amber-50',
        borderColor: 'border-amber-200',
        textColor: 'text-amber-800',
        title: '登录已过期',
        suggestion: '请重新登录后继续使用。您的会话凭证已失效。',
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
      return {
        icon: WifiOff,
        bgColor: 'bg-orange-50',
        borderColor: 'border-orange-200',
        textColor: 'text-orange-800',
        title: '服务暂时不可用',
        suggestion: '知识库服务当前响应异常，部分功能降级。请稍后重试。',
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
