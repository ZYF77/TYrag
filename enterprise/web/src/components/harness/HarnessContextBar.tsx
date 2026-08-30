import { useEffect, useState } from 'react';
import type { ConversationDetail, DisplayError, PatchConversationContextRequest } from '../../api/v2Types';

interface HarnessContextBarProps {
  conversation: ConversationDetail | null;
  saving: boolean;
  error: DisplayError | null;
  onSave: (context: PatchConversationContextRequest) => void;
}

/**
 * Compact session header above the chat column: current title + device badge
 * plus an inline "换绑" form backed by PATCH /conversations/{id}/context.
 * Replaces the old CONTEXT card / ContextEditor (no registry snapshot,
 * no registryVersion/contextVersion/faultCode display).
 */
export function HarnessContextBar({ conversation, saving, error, onSave }: HarnessContextBarProps) {
  const [open, setOpen] = useState(false);
  const [equipmentId, setEquipmentId] = useState('');
  const [fixedAssetNo, setFixedAssetNo] = useState('');

  useEffect(() => {
    setEquipmentId(conversation?.equipmentId ?? '');
    setFixedAssetNo(conversation?.fixedAssetNo ?? '');
    setOpen(false);
  }, [conversation]);

  const canSubmit = Boolean(equipmentId.trim() || fixedAssetNo.trim());

  return (
    <div className="harness-context-bar">
      {!conversation && <span className="console-empty">未选择会话。</span>}
      {conversation && (
        <>
          <div className="harness-context-summary">
            <strong>{conversation.title}</strong>
            <span data-testid="harness-device-badge" className="harness-device-badge">
              设备: {conversation.equipmentId ?? '未绑定设备'}
              {conversation.fixedAssetNo ? ` · ${conversation.fixedAssetNo}` : ''}
            </span>
          </div>
          <button
            type="button"
            onClick={() => setOpen((previous) => !previous)}
            aria-expanded={open}
            className="console-secondary-button"
          >
            换绑
          </button>
        </>
      )}
      {conversation && open && (
        <form
          aria-label="换绑设备"
          className="harness-context-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!canSubmit) return;
            // Omitted fields stay untouched server-side (PATCH merges only
            // provided fields); empty inputs therefore never clear a binding.
            onSave({
              ...(equipmentId.trim() ? { equipmentId: equipmentId.trim() } : {}),
              ...(fixedAssetNo.trim() ? { fixedAssetNo: fixedAssetNo.trim() } : {}),
            });
          }}
        >
          <label className="diag-field">
            equipmentId
            <input aria-label="equipmentId" value={equipmentId} onChange={(event) => setEquipmentId(event.target.value)} placeholder="新的 equipmentId" className="diag-input" />
          </label>
          <label className="diag-field">
            fixedAssetNo
            <input aria-label="fixedAssetNo" value={fixedAssetNo} onChange={(event) => setFixedAssetNo(event.target.value)} placeholder="新的 fixedAssetNo" className="diag-input" />
          </label>
          <p className="diag-help">仅提交填写的字段；留空字段保持不变。</p>
          <div className="console-card-actions">
            <button type="submit" disabled={saving || !canSubmit} className="console-primary-button">
              {saving ? '保存中…' : '保存换绑'}
            </button>
            <span className="console-route">PATCH /conversations/{conversation.conversationId}/context</span>
          </div>
          {error && <p className="console-alert">[{error.code}] {error.message}</p>}
        </form>
      )}
    </div>
  );
}
