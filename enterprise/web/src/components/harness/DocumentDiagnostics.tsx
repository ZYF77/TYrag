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
    <section aria-label="文档状态诊断" className="console-card">
      <div className="console-card-head">
        <div>
          <p className="console-eyebrow">Diagnostics</p>
          <h2>文档状态与质量诊断</h2>
          <p>只展示 Gateway v2 返回值，不推算百分比或 citation。</p>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading || !operation}
          className="console-secondary-button"
        >
          {loading ? '查询中…' : '轮询一次'}
        </button>
      </div>
      <div className="console-card-body">
        {!operation && <p className="console-empty">提交或查询一个文档后显示诊断。</p>}
        {operation && (
          <dl className="console-metrics">
            <div><dt>外部文档</dt><dd>{operation.externalDocumentId}</dd></div>
            <div><dt>版本</dt><dd>{operation.sourceVersionId}</dd></div>
            <div><dt>业务状态</dt><dd>{value(operation.businessStatus)}</dd></div>
            <div><dt>Parser stage</dt><dd>{value(operation.stage)}</dd></div>
            <div><dt>Readiness</dt><dd>{ready ? 'ready（Gateway 返回）' : `未声明 ready（${value(operation.status)}）`}</dd></div>
            <div><dt>Event status</dt><dd>{value(operation.eventStatus)}</dd></div>
            <div><dt>operationId / updatedAt</dt><dd>{operation.operationId} · {operation.updatedAt}</dd></div>
          </dl>
        )}
        {error && <p className="console-alert">[{error.code}] {error.message}</p>}
      </div>
    </section>
  );
}
