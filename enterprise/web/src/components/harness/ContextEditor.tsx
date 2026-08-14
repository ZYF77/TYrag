import { useEffect, useState } from 'react';
import type { ConversationDetail, DisplayError, PatchConversationContextRequest } from '../../api/v2Types';

interface ContextEditorProps {
  conversation: ConversationDetail | null;
  saving: boolean;
  error: DisplayError | null;
  onSave: (context: PatchConversationContextRequest) => void;
}

export function ContextEditor({ conversation, saving, error, onSave }: ContextEditorProps) {
  const [equipmentId, setEquipmentId] = useState('');
  const [fixedAssetNo, setFixedAssetNo] = useState('');
  const [faultCode, setFaultCode] = useState('');

  useEffect(() => {
    setEquipmentId(conversation?.equipmentId ?? '');
    setFixedAssetNo(conversation?.fixedAssetNo ?? '');
    setFaultCode(conversation?.faultCode ?? '');
  }, [conversation]);

  if (!conversation) {
    return <p className="console-empty">选择一个会话以切换 Asset context。</p>;
  }

  return (
    <form
      aria-label="Asset context 切换"
      className="harness-stack"
      onSubmit={(event) => {
        event.preventDefault();
        onSave({
          equipmentId: equipmentId.trim() || null,
          fixedAssetNo: fixedAssetNo.trim() || null,
          faultCode: faultCode.trim() || null,
        });
      }}
    >
      <div
        aria-label="Asset Registry canonical snapshot"
        className="diag-snapshot"
      >
        <p>Gateway 返回的 Asset Registry snapshot</p>
        <dl className="console-metrics">
          <div>
            <dt>equipmentId</dt>
            <dd>{conversation.context.equipmentId ?? '未解析'}</dd>
          </div>
          <div>
            <dt>fixedAssetNo</dt>
            <dd>{conversation.context.fixedAssetNo ?? '未解析'}</dd>
          </div>
        </dl>
      </div>
      <label className="diag-field">
        equipmentId
        <input aria-label="equipmentId" value={equipmentId} onChange={(event) => setEquipmentId(event.target.value)} placeholder="Asset Registry key" className="diag-input" />
      </label>
      <label className="diag-field">
        fixedAssetNo
        <input aria-label="fixedAssetNo" value={fixedAssetNo} onChange={(event) => setFixedAssetNo(event.target.value)} placeholder="Asset Registry key" className="diag-input" />
      </label>
      <label className="diag-field">
        faultCode
        <input aria-label="faultCode" value={faultCode} onChange={(event) => setFaultCode(event.target.value)} placeholder="server rules key" className="diag-input" />
      </label>
      <div className="console-card-actions">
        <button type="submit" disabled={saving} className="console-primary-button">{saving ? '保存中…' : '切换 Asset context'}</button>
        <span className="console-route">contextVersion: {conversation.contextVersion} · registryVersion: {conversation.context.registryVersion ?? '未提供'}</span>
      </div>
      {error && <p className="console-alert">[{error.code}] {error.message}</p>}
    </form>
  );
}
