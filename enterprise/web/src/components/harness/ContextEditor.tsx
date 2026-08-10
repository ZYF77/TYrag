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
    return <p className="text-xs text-slate-400">选择一个会话以切换 Asset context。</p>;
  }

  return (
    <form
      aria-label="Asset context 切换"
      className="space-y-2"
      onSubmit={(event) => {
        event.preventDefault();
        onSave({
          equipmentId: equipmentId.trim() || null,
          fixedAssetNo: fixedAssetNo.trim() || null,
          faultCode: faultCode.trim() || null,
        });
      }}
    >
      <div className="grid gap-2 sm:grid-cols-3">
        <input aria-label="equipmentId" value={equipmentId} onChange={(event) => setEquipmentId(event.target.value)} placeholder="equipmentId" className="rounded-md border border-slate-200 px-2.5 py-2 text-xs" />
        <input aria-label="fixedAssetNo" value={fixedAssetNo} onChange={(event) => setFixedAssetNo(event.target.value)} placeholder="fixedAssetNo" className="rounded-md border border-slate-200 px-2.5 py-2 text-xs" />
        <input aria-label="faultCode" value={faultCode} onChange={(event) => setFaultCode(event.target.value)} placeholder="faultCode" className="rounded-md border border-slate-200 px-2.5 py-2 text-xs" />
      </div>
      <div className="flex items-center gap-3">
        <button type="submit" disabled={saving} className="rounded-md bg-slate-800 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-900 disabled:opacity-50">{saving ? '保存中…' : '切换 Asset context'}</button>
        <span className="text-[11px] text-slate-500">contextVersion: {conversation.contextVersion} · registryVersion: {conversation.context.registryVersion ?? '未提供'}</span>
      </div>
      {error && <p className="text-xs text-rose-700">[{error.code}] {error.message}</p>}
    </form>
  );
}
