import { MessageSquare } from 'lucide-react';
import type { Conversation } from '../../api/types';

interface ConversationItemProps {
  conversation: Conversation;
  isActive: boolean;
  onClick: () => void;
}

export function ConversationItem({
  conversation,
  isActive,
  onClick,
}: ConversationItemProps) {
  return (
    <button
      onClick={onClick}
      className={`w-full text-left px-2.5 py-2 rounded-md transition-colors flex items-start gap-2 ${
        isActive
          ? 'bg-blue-50 text-blue-700'
          : 'text-gray-600 hover:bg-gray-100'
      }`}
      aria-label={`会话: ${conversation.title ?? conversation.conversationId}`}
    >
      <MessageSquare size={14} className="mt-0.5 flex-shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="text-xs font-medium truncate">
          {conversation.title ?? '新会话'}
        </p>
        <p className="text-[10px] text-gray-400 mt-0.5">
          {new Date(conversation.createdAt).toLocaleDateString('zh-CN')}
          {conversation.equipmentId && ` · ${conversation.equipmentId}`}
        </p>
      </div>
    </button>
  );
}
