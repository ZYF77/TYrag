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
      <div
        aria-label="Asset Registry canonical snapshot"
        className="rounded-md border border-indigo-100 bg-indigo-50/60 p-2 text-[11px] text-indigo-900"
      >
        <p className="font-medium">Gateway 返回的 Asset Registry snapshot</p>
        <dl className="mt-1 grid grid-cols-2 gap-x-3 gap-y-1">
          <div>
            <dt className="text-indigo-600">equipmentId</dt>
            <dd className="break-all font-medium">{conversation.context.equipmentId ?? '未解析'}</dd>
          </div>
          <div>
            <dt className="text-indigo-600">fixedAssetNo</dt>
            <dd className="break-all font-medium">{conversation.context.fixedAssetNo ?? '未解析'}</dd>
          </div>
        </dl>
      </div>
      <div className="grid gap-2 sm:grid-cols-3">
        <label className="text-[11px] text-slate-500">
          equipmentId
          <input aria-label="equipmentId" value={equipmentId} onChange={(event) => setEquipmentId(event.target.value)} placeholder="Asset Registry key" className="mt-1 w-full rounded-md border border-slate-200 px-2.5 py-2 text-xs" />
        </label>
        <label className="text-[11px] text-slate-500">
          fixedAssetNo
          <input aria-label="fixedAssetNo" value={fixedAssetNo} onChange={(event) => setFixedAssetNo(event.target.value)} placeholder="Asset Registry key" className="mt-1 w-full rounded-md border border-slate-200 px-2.5 py-2 text-xs" />
        </label>
        <label className="text-[11px] text-slate-500">
          faultCode
          <input aria-label="faultCode" value={faultCode} onChange={(event) => setFaultCode(event.target.value)} placeholder="server rules key" className="mt-1 w-full rounded-md border border-slate-200 px-2.5 py-2 text-xs" />
        </label>
      </div>
      <div className="flex items-center gap-3">
        <button type="submit" disabled={saving} className="rounded-md bg-slate-800 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-900 disabled:opacity-50">{saving ? '保存中…' : '切换 Asset context'}</button>
        <span className="text-[11px] text-slate-500">contextVersion: {conversation.contextVersion} · registryVersion: {conversation.context.registryVersion ?? '未提供'}</span>
      </div>
      {error && <p className="text-xs text-rose-700">[{error.code}] {error.message}</p>}
    </form>
  );
}
