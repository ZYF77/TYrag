import { Plus, RefreshCw, Settings } from 'lucide-react';
import type { Conversation } from '../../api/types';
import { ConversationItem } from '../layout/ConversationItem';
import { SidebarSection } from '../layout/SidebarSection';

interface DemoSidebarProps {
  conversations: Conversation[];
  activeId: string | null;
  loading: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRefresh: () => void;
  onOpenSettings: () => void;
}

export function DemoSidebar({
  conversations,
  activeId,
  loading,
  onSelect,
  onNew,
  onRefresh,
  onOpenSettings,
}: DemoSidebarProps) {
  return (
    <div className="flex flex-col h-full">
      <div className="p-3 border-b border-gray-200">
        <div className="flex items-center justify-between mb-2">
          <h1 className="text-sm font-semibold text-gray-700 tracking-tight">
            企业知识库
          </h1>
          <div className="flex items-center gap-1">
            <button
              onClick={onRefresh}
              className="p-1.5 rounded-md text-gray-400 hover:text-gray-600 hover:bg-gray-200 transition-colors"
              title="刷新列表"
              aria-label="刷新列表"
            >
              <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            </button>
            <button
              onClick={onOpenSettings}
              className="p-1.5 rounded-md text-gray-400 hover:text-gray-600 hover:bg-gray-200 transition-colors"
              title="联调设置"
              aria-label="联调设置"
            >
              <Settings size={14} />
            </button>
          </div>
        </div>

        <button
          onClick={onNew}
          className="w-full flex items-center justify-center gap-1.5 py-1.5 px-3 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 transition-colors"
          aria-label="新建会话"
        >
          <Plus size={14} />
          新建会话
        </button>
      </div>

      <SidebarSection title="历史会话">
        <div className="space-y-0.5">
          {loading && conversations.length === 0 && (
            <p className="text-xs text-gray-400 px-3 py-2">加载中...</p>
          )}
          {!loading && conversations.length === 0 && (
            <p className="text-xs text-gray-400 px-3 py-2">暂无会话记录</p>
          )}
          {conversations.map((conv) => (
            <ConversationItem
              key={conv.conversationId}
              conversation={conv}
              isActive={conv.conversationId === activeId}
              onClick={() => onSelect(conv.conversationId)}
            />
          ))}
        </div>
      </SidebarSection>
    </div>
  );
}
