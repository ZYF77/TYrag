import type { Citation, DisplayError } from '../../api/v2Types';

interface HarnessCitationPanelProps {
  citation: Citation | null;
  loading: boolean;
  error: DisplayError | null;
  onClose: () => void;
}

export function HarnessCitationPanel({ citation, loading, error, onClose }: HarnessCitationPanelProps) {
  return (
    <section aria-label="citation 详情" className="console-card">
      <div className="console-card-head">
        <div>
          <p className="console-eyebrow">Evidence</p>
          <h2>Citation snapshot</h2>
        </div>
        <button type="button" onClick={onClose} className="console-secondary-button">关闭</button>
      </div>
      <div className="console-card-body">
        {loading && <p className="console-empty">通过 Gateway 重新鉴权并加载中…</p>}
        {!loading && !citation && <p className="console-empty">选择回答中的 citation。</p>}
        {!loading && citation && (
          <dl className="console-metrics">
            <div><dt>title</dt><dd>{citation.title}</dd></div>
            <div><dt>sourceType</dt><dd>{citation.sourceType}</dd></div>
            <div><dt>externalDocumentId</dt><dd>{citation.externalDocumentId ?? 'null'}</dd></div>
            <div><dt>sourceVersionId</dt><dd>{citation.sourceVersionId ?? 'null'}</dd></div>
            <div><dt>assetId</dt><dd>{citation.assetId ?? 'null'}</dd></div>
            <div><dt>pageNo</dt><dd>{citation.pageNo ?? 'null'}</dd></div>
            {citation.recordId && <div><dt>recordId</dt><dd>{citation.recordId}</dd></div>}
          </dl>
        )}
        {!loading && citation && <p className="console-hint">{citation.excerpt ?? '未提供'}</p>}
        {error && <p className="console-alert">[{error.code}] {error.message}</p>}
      </div>
    </section>
  );
}
