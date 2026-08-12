import { useState, type FormEvent } from 'react';
import type { ConsoleModuleStatus } from '../../api/consoleTypes';
import type { ConversationAttachmentResponse, DisplayError } from '../../api/v2Types';

interface TransientAttachmentPanelProps {
  conversationId: string | null;
  loading: boolean;
  error: DisplayError | null;
  notice: string | null;
  onUpload: (file: File) => void;
  attachment?: ConversationAttachmentResponse | null;
  status?: ConsoleModuleStatus;
  issueLoading?: boolean;
  downloadLoading?: boolean;
  onIssueTicket?: () => void;
  onDownload?: () => void;
}

export function TransientAttachmentPanel({
  conversationId,
  loading,
  error,
  notice,
  onUpload,
  attachment = null,
  status,
  issueLoading = false,
  downloadLoading = false,
  onIssueTicket,
  onDownload,
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
        <div className="flex items-start justify-between gap-3">
          <h2 className="text-sm font-semibold text-slate-800">Transient attachment</h2>
          {status && (
            <span className="rounded-full bg-emerald-50 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-emerald-700">
              {status}
            </span>
          )}
        </div>
        <p className="mt-1 text-[11px] leading-5 text-slate-500">
          v2.1.0 public · create → ticket → download · conversation-scoped · indexPolicy=never。仅通过 Gateway，浏览器不直连对象存储。
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
      {attachment && (
        <div className="mt-3 rounded-lg border border-emerald-100 bg-emerald-50/60 p-3 text-[11px] text-emerald-950">
          <div className="flex items-center justify-between gap-2">
            <p className="font-semibold">Gateway 已签发临时附件</p>
            <span className="font-medium">indexPolicy: never</span>
          </div>
          <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1.5">
            <div><dt className="text-emerald-700/70">文件</dt><dd className="truncate font-medium">{attachment.fileName}</dd></div>
            <div><dt className="text-emerald-700/70">大小</dt><dd className="font-medium">{attachment.sizeBytes} bytes</dd></div>
            <div><dt className="text-emerald-700/70">下载次数</dt><dd className="font-medium">{attachment.downloadCount} / {attachment.maxDownloads}</dd></div>
            <div><dt className="text-emerald-700/70">票据到期</dt><dd className="font-medium">{attachment.ticketExpiresAt}</dd></div>
          </dl>
          <div className="mt-3 flex flex-wrap gap-2">
            {onIssueTicket && (
              <button type="button" onClick={onIssueTicket} disabled={issueLoading || downloadLoading} className="rounded-md border border-emerald-200 bg-white px-2.5 py-1.5 font-medium text-emerald-800 hover:bg-emerald-50 disabled:cursor-not-allowed disabled:opacity-50">
                {issueLoading ? '签发中…' : '重新签发票据'}
              </button>
            )}
            {onDownload && (
              <button type="button" onClick={onDownload} disabled={issueLoading || downloadLoading} className="rounded-md bg-emerald-700 px-2.5 py-1.5 font-medium text-white hover:bg-emerald-800 disabled:cursor-not-allowed disabled:opacity-50">
                {downloadLoading ? '验证中…' : '验证下载路由'}
              </button>
            )}
          </div>
          <p className="mt-2 text-emerald-800/70">Console 只展示元数据；不会显示票据、下载 URL 或附件内容。</p>
        </div>
      )}
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
