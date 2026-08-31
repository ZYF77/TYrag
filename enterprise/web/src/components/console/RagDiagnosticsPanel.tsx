import { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { toDisplayError, v2Api } from '../../api/v2Client';
import type {
  ConsoleState,
  RagDiagnosticTraceDetail,
  RagDiagnosticTracePage,
} from '../../api/consoleTypes';
import { formatTime, PanelCard, PanelError, panelErrorStatus } from './SystemSettingsPanels';

function initialState<T>(): ConsoleState<T> {
  return { status: 'processing', data: null, error: null };
}

export function RagDiagnosticsPanel() {
  const [list, setList] = useState<ConsoleState<RagDiagnosticTracePage>>(initialState);
  const [detail, setDetail] = useState<ConsoleState<RagDiagnosticTraceDetail>>({
    status: 'healthy',
    data: null,
    error: null,
  });
  const [runId, setRunId] = useState('');

  const loadList = useCallback(async () => {
    setList(initialState());
    try {
      const data = await v2Api.listRagDiagnosticTraces();
      setList({ status: 'healthy', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setList({ status: panelErrorStatus(displayError), data: null, error: displayError });
    }
  }, []);

  const loadDetail = useCallback(async (id: string) => {
    const value = id.trim();
    if (!value) return;
    setRunId(value);
    setDetail(initialState());
    try {
      const data = await v2Api.getRagDiagnosticTrace(value);
      setDetail({ status: 'healthy', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setDetail({ status: panelErrorStatus(displayError), data: null, error: displayError });
    }
  }, []);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  return (
    <PanelCard
      eyebrow="RAG diagnostics"
      title="RAG 诊断"
      description="按 runId 查看检索范围、候选排序、Context 取舍、LLM 时序与失败点。"
      status={detail.status !== 'healthy' ? detail.status : list.status}
      testId="rag-diagnostics-panel"
      actions={(
        <button type="button" className="console-icon-button" onClick={() => void loadList()} aria-label="刷新 RAG 诊断">
          <RefreshCw size={15} />
        </button>
      )}
    >
      <div className="console-toolbar">
        <form onSubmit={(event) => { event.preventDefault(); void loadDetail(runId); }}>
          <label htmlFor="rag-run-id">runId</label>
          <input id="rag-run-id" value={runId} onChange={(event) => setRunId(event.target.value)} placeholder="输入公共响应中的 runId" />
          <button type="submit" className="console-secondary-button">查询</button>
        </form>
      </div>

      {list.error && <PanelError error={list.error} onRetry={() => void loadList()} />}
      {list.data && (
        <div className="console-table-wrap">
          <table className="console-table">
            <thead><tr><th>runId</th><th>结果</th><th>耗时</th><th>时间</th><th>操作</th></tr></thead>
            <tbody>
              {list.data.items.map((item) => (
                <tr key={item.runId}>
                  <td className="console-table-mono">{item.runId}</td>
                  <td>{item.outcome ?? item.status}{item.truncated ? ' · truncated' : ''}</td>
                  <td>{item.durationMs == null ? '未提供' : `${Math.round(item.durationMs)}ms`}</td>
                  <td>{formatTime(item.createdAt)}</td>
                  <td><button type="button" className="console-secondary-button" onClick={() => void loadDetail(item.runId)}>查看</button></td>
                </tr>
              ))}
              {list.data.items.length === 0 && <tr><td colSpan={5}>暂无诊断记录。功能默认关闭，仅显示开启后产生的运行。</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {detail.error && <PanelError error={detail.error} onRetry={() => void loadDetail(runId)} />}
      {detail.data && (
        <div className="console-table-wrap">
          <table className="console-table">
            <thead><tr><th>阶段</th><th>相对时间</th><th>诊断数据</th></tr></thead>
            <tbody>
              {detail.data.diagnostics.events.map((event, index) => (
                <tr key={`${event.type}-${index}`}>
                  <td>{event.type}</td>
                  <td>{Math.round(event.atMs)}ms</td>
                  <td><pre className="console-table-mono">{JSON.stringify(event.data, null, 2)}</pre></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </PanelCard>
  );
}
