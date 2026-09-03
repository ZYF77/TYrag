import { useCallback, useEffect, useState } from 'react';
import { ChevronRight, RefreshCw, X } from 'lucide-react';
import { toDisplayError, v2Api } from '../../api/v2Client';
import type {
  AdminChunk,
  ChunkPage,
  DocumentMetadataDetail,
  DocumentMetadataItem,
} from '../../api/consoleTypes';
import type { DisplayError } from '../../api/v2Types';
import { ConsoleOverlay } from './ConsoleOverlay';
import { DEFAULT_PAGE_SIZE, PaginationBar } from './ConsoleTableControls';

interface DocumentInspectorProps {
  document: DocumentMetadataItem;
  onClose: () => void;
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '未提供';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

function value(value: unknown): string {
  if (value === null || value === undefined || value === '') return '未提供';
  return String(value);
}

function parserMethod(method: string | null | undefined): string {
  if (!method) return '未读取';
  const normalized = method.toLowerCase();
  if (normalized === 'naive') return 'DeepDOC / PDF 文档';
  if (normalized === 'picture') return '图片解析';
  if (normalized === 'table') return '表格解析';
  return method;
}

function jsonPreview(data: unknown): string {
  try {
    return JSON.stringify(data ?? {}, null, 2);
  } catch {
    return '{}';
  }
}

function ChunkDetail({ chunk, onClose }: { chunk: AdminChunk; onClose: () => void }) {
  return (
    <ConsoleOverlay open mode="dialog" onClose={onClose} ariaLabel="Chunk 详情" className="console-chunk-detail-overlay">
      <section className="console-detail-dialog" aria-label="Chunk 详情">
        <header className="console-detail-dialog-head">
          <div>
            <p className="console-eyebrow">Parsed chunk</p>
            <h2>Chunk 详情</h2>
            <p className="console-route">{chunk.id}</p>
          </div>
          <button type="button" className="console-icon-button" aria-label="关闭 Chunk 详情" onClick={onClose}><X size={17} /></button>
        </header>
        <div className="console-detail-dialog-body">
          <dl className="console-detail-facts">
            <div><dt>文档 ID</dt><dd>{chunk.documentId}</dd></div>
            <div><dt>文档类型</dt><dd>{value(chunk.docType)}</dd></div>
            <div><dt>图片 ID</dt><dd>{value(chunk.imageId)}</dd></div>
            <div><dt>可用状态</dt><dd>{value(chunk.available)}</dd></div>
          </dl>
          <section className="console-detail-section">
            <div className="console-detail-section-head"><strong>解析内容</strong><span>content</span></div>
            <div className="console-chunk-content">{chunk.content || '未提供解析内容。'}</div>
          </section>
          <section className="console-detail-section">
            <div className="console-detail-section-head"><strong>位置</strong><span>positions</span></div>
            <pre className="console-json-preview">{jsonPreview(chunk.positions)}</pre>
          </section>
        </div>
      </section>
    </ConsoleOverlay>
  );
}

export function DocumentInspector({ document, onClose }: DocumentInspectorProps) {
  const [detail, setDetail] = useState<DocumentMetadataDetail | null>(null);
  const [detailError, setDetailError] = useState<DisplayError | null>(null);
  const [chunks, setChunks] = useState<ChunkPage | null>(null);
  const [chunkError, setChunkError] = useState<DisplayError | null>(null);
  const [chunkPage, setChunkPage] = useState(1);
  const [chunkPageSize, setChunkPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [selectedChunk, setSelectedChunk] = useState<AdminChunk | null>(null);

  const loadDetail = useCallback(async () => {
    setDetailError(null);
    try {
      setDetail(await v2Api.getAdminDocumentMetadataDetail(document.externalDocumentId, document.sourceVersionId));
    } catch (error) {
      setDetailError(toDisplayError(error));
    }
  }, [document.externalDocumentId, document.sourceVersionId]);

  const loadChunks = useCallback(async () => {
    setChunkError(null);
    try {
      setChunks(await v2Api.listAdminDocumentChunks(document.externalDocumentId, {
        sourceVersionId: document.sourceVersionId,
        page: chunkPage,
        pageSize: chunkPageSize,
      }));
    } catch (error) {
      setChunkError(toDisplayError(error));
    }
  }, [chunkPage, chunkPageSize, document.externalDocumentId, document.sourceVersionId]);

  useEffect(() => { void loadDetail(); }, [loadDetail]);
  useEffect(() => { void loadChunks(); }, [loadChunks]);

  const item = detail?.item ?? document;
  const parser = detail?.parser;
  const metadata = detail?.metadata;

  return (
    <ConsoleOverlay open mode="dialog" onClose={onClose} ariaLabel="文件详情" className="console-document-inspector-overlay">
      <section className="console-detail-dialog console-document-inspector" data-testid="console-document-inspector">
        <header className="console-detail-dialog-head">
          <div>
            <p className="console-eyebrow">Document metadata</p>
            <h2>{item.fileName}</h2>
            <p className="console-route">{item.externalDocumentId} · {item.sourceVersionId}</p>
          </div>
          <div className="console-detail-dialog-actions">
            <button type="button" className="console-icon-button" aria-label="刷新文件详情" onClick={() => { void loadDetail(); void loadChunks(); }}><RefreshCw size={16} /></button>
            <button type="button" className="console-icon-button" aria-label="关闭文件详情" onClick={onClose}><X size={17} /></button>
          </div>
        </header>
        <div className="console-detail-dialog-body">
          {detailError && <div className="console-alert" role="alert">[{detailError.code}] {detailError.message}</div>}
          <section className="console-detail-section">
            <div className="console-detail-section-head"><strong>文档属性</strong><span>metadata</span></div>
            <dl className="console-detail-facts">
              <div><dt>来源系统</dt><dd>{value(item.sourceSystem)}</dd></div>
              <div><dt>来源类型</dt><dd>{value(item.sourceKind)}</dd></div>
              <div><dt>文档类型</dt><dd>{value(item.documentType)}</dd></div>
              <div><dt>当前版本</dt><dd>{value(item.currentVersion)}</dd></div>
              <div><dt>设备编号</dt><dd>{value(item.equipmentId)}</dd></div>
              <div><dt>固定资产</dt><dd>{value(item.fixedAssetNo)}</dd></div>
              <div><dt>同步状态</dt><dd>{value(item.syncStatus)}</dd></div>
              <div><dt>业务状态</dt><dd>{value(item.businessStatus)}</dd></div>
              <div><dt>创建时间</dt><dd>{formatTime(item.createdAt)}</dd></div>
              <div><dt>解析完成</dt><dd>{formatTime(item.parsedAt)}</dd></div>
              <div><dt>RAGFlow Dataset</dt><dd>{value(item.ragflowDatasetId)}</dd></div>
              <div><dt>RAGFlow Document</dt><dd>{value(item.ragflowDocumentId)}</dd></div>
              {metadata && <>
                <div><dt>源文件大小</dt><dd>{value(item.sourceSize)}</dd></div>
                <div><dt>源页数</dt><dd>{value(metadata.sourcePageCount)}</dd></div>
                <div><dt>解析尝试</dt><dd>{value(metadata.attemptCount)}</dd></div>
                <div><dt>最后错误码</dt><dd>{value(metadata.lastErrorCode)}</dd></div>
              </>}
            </dl>
          </section>

          <section className="console-detail-section">
            <div className="console-detail-section-head"><strong>解析方式</strong><span>parser</span></div>
            {parser ? (
              <>
                <dl className="console-detail-facts">
                  <div><dt>应用状态</dt><dd>{value(parser.applicationStatus)}</dd></div>
                  <div><dt>配置 Profile</dt><dd>{value(parser.profile)} · {value(parser.profileVersion)}</dd></div>
                  <div><dt>RAGFlow 方法</dt><dd>{parserMethod(parser.ragflow?.chunkMethod)}</dd></div>
                  <div><dt>RAGFlow 状态</dt><dd>{value(parser.ragflow?.run ?? parser.errorCode)}</dd></div>
                  <div><dt>Chunk 数</dt><dd>{value(parser.ragflow?.chunkCount)}</dd></div>
                  <div><dt>Token 数</dt><dd>{value(parser.ragflow?.tokenCount)}</dd></div>
                </dl>
                {parser.errorCode && <p className="console-help-text">RAGFlow 读取状态：{parser.errorCode}</p>}
                <details className="console-json-details">
                  <summary>查看解析配置</summary>
                  <pre className="console-json-preview">{jsonPreview({ expected: parser.expected, configured: parser.configured, executed: parser.executed, ragflow: parser.ragflow?.parserConfig })}</pre>
                </details>
              </>
            ) : detailError ? (
              <p className="console-empty">解析方式暂不可用，请稍后重试。</p>
            ) : <p className="console-empty">解析信息加载中…</p>}
          </section>

          <section className="console-detail-section">
            <div className="console-detail-section-head"><strong>解析 Chunk</strong><span>{chunks ? `${chunks.total} 个` : chunkError ? '不可用' : '加载中'}</span></div>
            {chunkError && <div className="console-alert" role="alert">[{chunkError.code}] {chunkError.message}</div>}
            {chunks?.state === 'not_ready' && <p className="console-empty">该文档尚未完成 RAGFlow 解析。</p>}
            {chunks && chunks.items.length > 0 && (
              <div className="console-chunk-list" role="list" aria-label="解析 Chunk 列表">
                {chunks.items.map((chunk) => (
                  <button key={chunk.id} type="button" className="console-chunk-row" onClick={() => setSelectedChunk(chunk)}>
                    <span className="console-chunk-row-id">{chunk.id}</span>
                    <span className="console-chunk-row-content">{chunk.content || '未提供解析内容'}</span>
                    <ChevronRight size={15} aria-hidden="true" />
                  </button>
                ))}
              </div>
            )}
            {chunks && chunks.items.length === 0 && chunks.state !== 'not_ready' && !chunkError && <p className="console-empty">没有可显示的 Chunk。</p>}
            {chunks && (
              <PaginationBar
                page={chunkPage}
                itemCount={chunks.items.length}
                total={chunks.total}
                hasMore={chunks.hasMore}
                pageSize={chunkPageSize}
                onPageSizeChange={(value) => { setChunkPageSize(value); setChunkPage(1); }}
                onPrevious={() => setChunkPage((page) => Math.max(1, page - 1))}
                onNext={() => setChunkPage((page) => page + 1)}
              />
            )}
          </section>
        </div>
      </section>
      {selectedChunk && <ChunkDetail chunk={selectedChunk} onClose={() => setSelectedChunk(null)} />}
    </ConsoleOverlay>
  );
}
