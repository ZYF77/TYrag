import { Cpu } from 'lucide-react';
import type { Conversation } from '../../api/types';

interface DeviceContextCardProps {
  conversation: Conversation | null;
}

export function DeviceContextCard({ conversation }: DeviceContextCardProps) {
  const hasContext =
    conversation?.equipmentId ||
    conversation?.fixedAssetNo ||
    conversation?.faultCode;

  return (
    <div className="px-4 py-3 border-b border-gray-100 bg-white">
      <div className="flex items-center gap-2">
        <Cpu size={14} className="text-gray-400 flex-shrink-0" />
        <span className="text-xs font-medium text-gray-500">当前设备上下文</span>
        {!hasContext && (
          <span className="text-xs text-gray-300">未绑定设备</span>
        )}
        {conversation?.equipmentId && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 text-xs font-medium">
            {conversation.equipmentId}
          </span>
        )}
        {conversation?.fixedAssetNo && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-green-50 text-green-700 text-xs font-medium">
            固定资产: {conversation.fixedAssetNo}
          </span>
        )}
        {conversation?.faultCode && (
          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-red-50 text-red-700 text-xs font-medium">
            故障码: {conversation.faultCode}
          </span>
        )}
      </div>
    </div>
  );
}
