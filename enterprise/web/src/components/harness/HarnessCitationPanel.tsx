import type { Citation, DisplayError } from '../../api/v2Types';

interface HarnessCitationPanelProps {
  citation: Citation | null;
  loading: boolean;
  error: DisplayError | null;
  onClose: () => void;
}

export function HarnessCitationPanel({ citation, loading, error, onClose }: HarnessCitationPanelProps) {
  return (
    <section aria-label="citation 详情" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-800">Citation snapshot</h2>
        <button type="button" onClick={onClose} className="text-xs text-slate-500 hover:text-slate-800">关闭</button>
      </div>
      {loading && <p className="text-xs text-slate-400">通过 Gateway 重新鉴权并加载中…</p>}
      {!loading && !citation && <p className="text-xs text-slate-400">选择回答中的 citation。</p>}
      {!loading && citation && (
        <dl className="space-y-2 text-xs">
          <div><dt className="text-slate-500">title</dt><dd className="mt-0.5 font-medium text-slate-800">{citation.title}</dd></div>
          <div><dt className="text-slate-500">sourceType</dt><dd className="mt-0.5 text-slate-800">{citation.sourceType}</dd></div>
          <div><dt className="text-slate-500">externalDocumentId</dt><dd className="mt-0.5 break-all text-slate-800">{citation.externalDocumentId ?? 'null'}</dd></div>
          <div><dt className="text-slate-500">sourceVersionId</dt><dd className="mt-0.5 break-all text-slate-800">{citation.sourceVersionId ?? 'null'}</dd></div>
          <div><dt className="text-slate-500">assetId</dt><dd className="mt-0.5 break-all text-slate-800">{citation.assetId ?? 'null'}</dd></div>
          <div><dt className="text-slate-500">pageNo</dt><dd className="mt-0.5 text-slate-800">{citation.pageNo ?? 'null'}</dd></div>
          <div><dt className="text-slate-500">excerpt</dt><dd className="mt-0.5 whitespace-pre-wrap leading-5 text-slate-800">{citation.excerpt ?? '未提供'}</dd></div>
          {citation.recordId && <div><dt className="text-slate-500">recordId</dt><dd className="mt-0.5 text-slate-800">{citation.recordId}</dd></div>}
        </dl>
      )}
      {error && <p className="mt-3 text-xs text-rose-700">[{error.code}] {error.message}</p>}
    </section>
  );
}
