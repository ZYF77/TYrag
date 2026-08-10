import { useState, type FormEvent } from 'react';
import type { DisplayError } from '../../api/v2Types';

interface TransientAttachmentPanelProps {
  conversationId: string | null;
  loading: boolean;
  error: DisplayError | null;
  notice: string | null;
  onUpload: (file: File) => void;
}

export function TransientAttachmentPanel({
  conversationId,
  loading,
  error,
  notice,
  onUpload,
}: TransientAttachmentPanelProps) {
  const [file, setFile] = useState<File | null>(null);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (file && conversationId && !loading) onUpload(file);
  };

  return (
    <section
      aria-label="transient attachment 边界"
      className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm"
    >
      <div className="mb-3">
        <h2 className="text-sm font-semibold text-slate-800">Transient attachment 边界</h2>
        <p className="mt-1 text-[11px] leading-5 text-slate-500">
          P1 planned · conversation-scoped · indexPolicy=never · TTL 24h。仅通过 Gateway，浏览器不直连对象存储。
        </p>
      </div>
      <form className="space-y-2" onSubmit={submit}>
        <input
          aria-label="transient attachment file"
          type="file"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          disabled={!conversationId || loading}
          className="block w-full min-w-0 text-xs text-slate-600 file:mr-2 file:rounded-md file:border-0 file:bg-slate-100 file:px-2.5 file:py-1.5 file:text-xs file:text-slate-700"
        />
        <div className="flex items-center justify-between gap-2">
          <span className="min-w-0 truncate text-[11px] text-slate-400">
            {file ? `${file.name} · ${file.type || 'application/octet-stream'}` : '选择文件后仅发送 base64 内容'}
          </span>
          <button
            type="submit"
            disabled={!conversationId || !file || loading}
            className="shrink-0 rounded-md border border-slate-200 px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? '提交中…' : '通过 Gateway 提交'}
          </button>
        </div>
      </form>
      {!conversationId && <p className="mt-2 text-[11px] text-slate-400">先选择会话。</p>}
      {notice && <p className="mt-2 text-xs text-emerald-700">{notice}</p>}
      {error && (
        <p role="alert" className="mt-2 text-xs text-rose-700">
          <span>[{error.code}]</span> <span>{error.message}</span>
          {error.httpStatus ? <span> · HTTP {error.httpStatus}</span> : null}
        </p>
      )}
    </section>
  );
}
