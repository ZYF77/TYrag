import { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { toDisplayError, v2Api } from '../../api/v2Client';
import type { ConsoleModuleStatus, ConsoleState, DocumentMetadataItem, DocumentMetadataPage } from '../../api/consoleTypes';
import { DEFAULT_PAGE_SIZE, PaginationBar } from './ConsoleTableControls';
import { DocumentInspector } from './DocumentInspector';
import { PanelCard, PanelError, panelErrorStatus, StatusPill, formatTime } from './SystemSettingsPanels';

function initialState(): ConsoleState<DocumentMetadataPage> {
  return { status: 'processing', data: null, error: null };
}

export function ChunkManagementPanel() {
  const [state, setState] = useState(initialState);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [selected, setSelected] = useState<DocumentMetadataItem | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);

  const load = useCallback(async () => {
    setState(initialState());
    try {
      const data = await v2Api.listAdminDocumentMetadata({
        limit: pageSize,
        offset: (page - 1) * pageSize,
        parserApplicationStatus: 'executed',
        orderBy: 'updatedAt',
        order: 'desc',
      });
      setState({ status: 'healthy', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setState({ status: panelErrorStatus(displayError), data: null, error: displayError });
    }
  }, [page, pageSize]);

  useEffect(() => { void load(); }, [load, refreshToken]);

  const data = state.data;
  const status: ConsoleModuleStatus = state.status;

  return (
    <>
      <PanelCard
        eyebrow="Parsed chunks"
        title="解析 Chunk"
        description="按文档查看 RAGFlow 已解析的 Chunk、解析方式和位置；点击任意行打开详情。"
        status={status}
        className="console-table-card"
        testId="console-meta-chunks-card"
        actions={<button type="button" className="console-icon-button" aria-label="刷新解析 Chunk" onClick={() => setRefreshToken((token) => token + 1)}><RefreshCw size={16} /></button>}
      >
        {data?.items.length ? (
          <div className="console-table-wrap">
            <table className="console-table" data-testid="console-meta-chunks-table">
              <thead><tr><th>文档</th><th>来源</th><th>解析 Profile</th><th>状态</th><th>解析时间</th></tr></thead>
              <tbody>
                {data.items.map((item) => (
                  <tr key={`${item.externalDocumentId}-${item.sourceVersionId}`} data-row-action="true" tabIndex={0} onClick={() => setSelected(item)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setSelected(item); } }}>
                    <td><strong>{item.fileName}</strong><small className="console-route">{item.externalDocumentId} · {item.sourceVersionId}</small></td>
                    <td>{item.sourceSystem}</td>
                    <td>{item.parserProfile ?? '未提供'}</td>
                    <td><StatusPill code={item.parserApplicationStatus ?? 'executed'} /></td>
                    <td>{formatTime(item.parsedAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="console-empty">{state.status === 'processing' ? '解析文档加载中…' : '暂无已解析文档。'}</p>
        )}
        <PaginationBar
          page={page}
          itemCount={data?.items.length ?? 0}
          hasMore={Boolean(data?.hasMore)}
          pageSize={pageSize}
          onPageSizeChange={(value) => { setPageSize(value); setPage(1); }}
          onPrevious={() => setPage((current) => Math.max(1, current - 1))}
          onNext={() => setPage((current) => current + 1)}
        />
        {state.error && <PanelError error={state.error} onRetry={() => setRefreshToken((token) => token + 1)} />}
      </PanelCard>
      {selected && <DocumentInspector document={selected} onClose={() => setSelected(null)} />}
    </>
  );
}
