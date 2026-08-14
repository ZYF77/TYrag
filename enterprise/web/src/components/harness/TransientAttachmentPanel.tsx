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
      className="console-card"
    >
      <div className="mb-3 console-card-head">
        <div>
          <p className="console-eyebrow">Attachment</p>
          <h2>Transient attachment</h2>
          <p>v2.1.0 public · create → ticket → download · conversation-scoped · indexPolicy=never。仅通过 Gateway，浏览器不直连对象存储。</p>
        </div>
        {status && <span className="console-mode-badge">{status}</span>}
      </div>
      <div className="console-card-body">
        <form className="console-pad harness-stack" onSubmit={submit}>
          <input
            aria-label="transient attachment file"
            type="file"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            disabled={!conversationId || loading}
          />
          <div className="console-card-actions">
            <span className="console-route">
              {file ? `${file.name} · ${file.type || 'application/octet-stream'}` : '选择文件后仅发送 base64 内容'}
            </span>
            <button
              type="submit"
              disabled={!conversationId || !file || loading}
              className="console-secondary-button"
            >
              {loading ? '提交中…' : '通过 Gateway 提交'}
            </button>
          </div>
        </form>
        {!conversationId && <p className="console-hint">先选择会话。</p>}
        {attachment && (
          <div className="diag-snapshot">
            <div className="console-card-actions">
              <p>Gateway 已签发临时附件</p>
              <span className="console-route">indexPolicy: never</span>
            </div>
            <dl className="console-metrics">
              <div><dt>文件</dt><dd>{attachment.fileName}</dd></div>
              <div><dt>大小</dt><dd>{attachment.sizeBytes} bytes</dd></div>
              <div><dt>下载次数</dt><dd>{attachment.downloadCount} / {attachment.maxDownloads}</dd></div>
              <div><dt>票据到期</dt><dd>{attachment.ticketExpiresAt}</dd></div>
            </dl>
            <div className="console-chip-row">
              {onIssueTicket && (
                <button type="button" onClick={onIssueTicket} disabled={issueLoading || downloadLoading} className="console-secondary-button">
                  {issueLoading ? '签发中…' : '重新签发票据'}
                </button>
              )}
              {onDownload && (
                <button type="button" onClick={onDownload} disabled={issueLoading || downloadLoading} className="console-primary-button">
                  {downloadLoading ? '验证中…' : '验证下载路由'}
                </button>
              )}
            </div>
            <p className="diag-help">Console 只展示元数据；不会显示票据、下载 URL 或附件内容。</p>
          </div>
        )}
        {notice && <p className="console-hint">{notice}</p>}
        {error && (
          <p role="alert" className="console-alert">
            <span>[{error.code}]</span> <span>{error.message}</span>
            {error.httpStatus ? <span> · HTTP {error.httpStatus}</span> : null}
          </p>
        )}
      </div>
    </section>
  );
}
