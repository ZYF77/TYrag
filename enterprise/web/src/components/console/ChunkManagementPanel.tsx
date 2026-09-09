import { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { toDisplayError, v2Api } from '../../api/v2Client';
import type { ConsoleModuleStatus, ConsoleState, DocumentMetadataItem, DocumentMetadataPage } from '../../api/consoleTypes';
import { DEFAULT_PAGE_SIZE, PaginationBar } from './ConsoleTableControls';
import { DocumentInspector } from './DocumentInspector';
import { EmptyState } from '../common/EmptyState';
import { PanelCard, PanelError, panelErrorStatus } from '../common/Panel';
import { StatusPill } from '../common/StatusPill';
import { formatTime } from '../../lib/format';

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
        // 「已解析可检索」：默认按 sync_status=ready（有 ragflowDocumentId 的活文档）。
        // 不强制 parserApplicationStatus=executed——线上 GQ 文档多为 legacy_unverified。
        status: 'ready',
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
        description="按「已解析可检索」列出 sync_status=ready 的文档（不要求 parser executed）；点击行查看 Chunk、解析方式和位置。"
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
                    <td><StatusPill code={item.syncStatus ?? 'ready'} /></td>
                    <td>{formatTime(item.parsedAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            loading={state.status === 'processing'}
            loadingText="解析文档加载中…"
            emptyText="暂无已解析可检索文档（sync_status=ready）。"
          />
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
