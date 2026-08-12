import { Plus, RefreshCw, FileText } from 'lucide-react';
import type { Conversation } from '../../api/types';
import { SidebarSection } from './SidebarSection';
import { ConversationItem } from './ConversationItem';

interface SidebarProps {
  conversations: Conversation[];
  activeId: string | null;
  loading: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRefresh: () => void;
  syncCount: number;
  onToggleSyncDrawer: () => void;
  syncDrawerOpen: boolean;
}

export function Sidebar({
  conversations,
  activeId,
  loading,
  onSelect,
  onNew,
  onRefresh,
  syncCount,
  onToggleSyncDrawer,
  syncDrawerOpen,
}: SidebarProps) {
  return (
    <div className="flex flex-col h-full">
      {/* Header */}
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
              onClick={onToggleSyncDrawer}
              className={`p-1.5 rounded-md transition-colors ${
                syncDrawerOpen
                  ? 'text-blue-600 bg-blue-50'
                  : 'text-gray-400 hover:text-gray-600 hover:bg-gray-200'
              }`}
              title="文件同步状态"
              aria-label="文件同步状态"
            >
              <FileText size={14} />
              {syncCount > 0 && (
                <span className="absolute -mt-1 -ml-1 inline-flex items-center justify-center w-4 h-4 text-[10px] font-medium text-white bg-blue-500 rounded-full">
                  {syncCount}
                </span>
              )}
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
        <a
          href="/console"
          data-testid="console-nav-link"
          className="mt-2 flex items-center justify-center rounded-md border border-gray-200 bg-white py-1.5 px-3 text-xs font-medium text-gray-600 hover:border-teal-200 hover:bg-teal-50 hover:text-teal-700"
        >
          打开联调 Console
        </a>
      </div>

      {/* Conversation list */}
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
