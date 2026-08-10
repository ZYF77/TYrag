import type { DisplayError, DocumentOperation } from '../../api/v2Types';

interface DocumentDiagnosticsProps {
  operation: DocumentOperation | null;
  loading: boolean;
  error: DisplayError | null;
  onRefresh: () => void;
}

function value(value: string | boolean | null | undefined): string {
  if (value === null || value === undefined || value === '') return '未提供';
  return String(value);
}

export function DocumentDiagnostics({
  operation,
  loading,
  error,
  onRefresh,
}: DocumentDiagnosticsProps) {
  const ready = operation?.status === 'ready';
  return (
    <section aria-label="文档状态诊断" className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-800">文档状态与质量诊断</h2>
          <p className="mt-1 text-[11px] text-slate-500">只展示 Gateway v2 返回值，不推算百分比或 citation。</p>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading || !operation}
          className="rounded-md border border-slate-200 px-2.5 py-1.5 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? '查询中…' : '轮询一次'}
        </button>
      </div>
      {!operation && <p className="text-xs text-slate-400">提交或查询一个文档后显示诊断。</p>}
      {operation && (
        <dl className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
          <div className="rounded-md bg-slate-50 p-2"><dt className="text-slate-500">外部文档</dt><dd className="mt-1 break-all font-medium text-slate-800">{operation.externalDocumentId}</dd></div>
          <div className="rounded-md bg-slate-50 p-2"><dt className="text-slate-500">版本</dt><dd className="mt-1 font-medium text-slate-800">{operation.sourceVersionId}</dd></div>
          <div className="rounded-md bg-slate-50 p-2"><dt className="text-slate-500">业务状态</dt><dd className="mt-1 font-medium text-slate-800">{value(operation.businessStatus)}</dd></div>
          <div className="rounded-md bg-slate-50 p-2"><dt className="text-slate-500">Parser stage</dt><dd className="mt-1 font-medium text-slate-800">{value(operation.stage)}</dd></div>
          <div className="rounded-md bg-slate-50 p-2"><dt className="text-slate-500">Readiness</dt><dd className={`mt-1 font-medium ${ready ? 'text-emerald-700' : 'text-amber-700'}`}>{ready ? 'ready（Gateway 返回）' : `未声明 ready（${value(operation.status)}）`}</dd></div>
          <div className="rounded-md bg-slate-50 p-2"><dt className="text-slate-500">Event status</dt><dd className="mt-1 font-medium text-slate-800">{value(operation.eventStatus)}</dd></div>
          <div className="col-span-2 rounded-md bg-slate-50 p-2 sm:col-span-3"><dt className="text-slate-500">operationId / updatedAt</dt><dd className="mt-1 break-all font-medium text-slate-800">{operation.operationId} · {operation.updatedAt}</dd></div>
        </dl>
      )}
      {error && <p className="mt-3 text-xs text-rose-700">[{error.code}] {error.message}</p>}
    </section>
  );
}
