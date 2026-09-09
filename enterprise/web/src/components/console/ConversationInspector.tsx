import { X } from 'lucide-react';
import type { ConversationMetadataItem } from '../../api/consoleTypes';
import { DialogHeader } from '../common/DialogHeader';
import { StatusPill } from '../common/StatusPill';
import { formatTime } from '../../lib/format';
import { ConsoleOverlay } from './ConsoleOverlay';

function value(raw: unknown): string {
  if (raw === null || raw === undefined || raw === '') return '未提供';
  return String(raw);
}

export function ConversationInspector({ conversation, onClose }: { conversation: ConversationMetadataItem; onClose: () => void }) {
  return (
    <ConsoleOverlay open mode="dialog" onClose={onClose} ariaLabel="会话目录详情" className="console-conversation-inspector-overlay">
      <section className="console-detail-dialog console-conversation-inspector" data-testid="console-conversation-inspector">
        <DialogHeader
          eyebrow="Conversation directory"
          title="会话目录详情"
          meta={<p className="console-route">{conversation.conversationId}</p>}
          closeLabel="关闭会话目录详情"
          onClose={onClose}
          closeContent={<X size={17} />}
        />
        <div className="console-detail-dialog-body console-conversation-inspector-body">
          <div className="console-info-banner" role="note">
            <div>
              <strong>这里显示什么？</strong>
              <p>会话目录/列表的元数据字段；不回显问答正文。查看完整问答请到会话管理。</p>
            </div>
          </div>
          <div className="console-chip-row">
            <span className="console-chip">业务用户 · {conversation.businessUserId}</span>
            {conversation.equipmentId && <span className="console-chip">设备 · {conversation.equipmentId}</span>}
            {conversation.fixedAssetNo && <span className="console-chip">固定资产 · {conversation.fixedAssetNo}</span>}
            <StatusPill code={conversation.status} />
          </div>
          <section className="console-detail-section">
            <div className="console-detail-section-head"><strong>目录字段</strong><span>metadata</span></div>
            <dl className="console-detail-facts">
              <div><dt>会话 ID</dt><dd>{value(conversation.conversationId)}</dd></div>
              <div><dt>业务用户</dt><dd>{value(conversation.businessUserId)}</dd></div>
              <div><dt>设备</dt><dd>{value(conversation.equipmentId)}</dd></div>
              <div><dt>固定资产</dt><dd>{value(conversation.fixedAssetNo)}</dd></div>
              <div><dt>状态</dt><dd>{value(conversation.status)}</dd></div>
              <div><dt>Context 版本</dt><dd>{conversation.contextVersion != null ? `v${conversation.contextVersion}` : '未提供'}</dd></div>
              <div><dt>RAGFlow Chat</dt><dd>{value(conversation.ragflowChatId)}</dd></div>
              <div><dt>RAGFlow Session</dt><dd>{value(conversation.ragflowSessionId)}</dd></div>
              <div><dt>创建时间</dt><dd>{formatTime(conversation.createdAt)}</dd></div>
              <div><dt>最近消息</dt><dd>{formatTime(conversation.lastMessageAt)}</dd></div>
            </dl>
          </section>
        </div>
      </section>
    </ConsoleOverlay>
  );
}
