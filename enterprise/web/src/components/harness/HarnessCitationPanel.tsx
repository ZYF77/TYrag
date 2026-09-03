import ReactMarkdown from 'react-markdown';
import type { Citation, DisplayError } from '../../api/v2Types';

interface HarnessCitationPanelProps {
  citation: Citation | null;
  citations?: Citation[];
  loading: boolean;
  error: DisplayError | null;
  onClose: () => void;
  onSelectCitation?: (citation: Citation) => void;
}

function sourceTypeLabel(sourceType: Citation['sourceType']): string {
  if (sourceType === 'document') return 'RAGFlow 源文档';
  if (sourceType === 'business_record') return '业务记录';
  if (sourceType === 'timeseries') return '时序数据';
  return '联网来源';
}

function isInlineFigure(citation: Citation): boolean {
  return citation.sourceType === 'document' && citation.fileKind === 'crop' && Boolean(citation.downloadUrl);
}

export function HarnessCitationPanel({ citation, citations = [], loading, error, onClose, onSelectCitation }: HarnessCitationPanelProps) {
  const inlineFigure = citation ? isInlineFigure(citation) : false;

  return (
    <section aria-label="citation 详情" className="console-card harness-citation-card">
      <div className="console-card-head">
        <div>
          <p className="console-eyebrow">证据</p>
          <h2>引用详情</h2>
          <p>只展示当前回答实际引用的唯一来源；同一源文档的多个 chunk 会合并。</p>
        </div>
        <button type="button" onClick={onClose} className="console-secondary-button">关闭</button>
      </div>
      <div className="console-card-body harness-citation-body">
        {loading && <p className="console-empty">通过 Gateway 重新鉴权并加载中…</p>}
        {!loading && !citation && <p className="console-empty">选择回答中的引用。</p>}
        {!loading && citation && (
          <>
            {citations.length > 1 && (
              <div className="harness-citation-group" aria-label="引用片段组">
                <div className="harness-citation-group-head">
                  <strong>引用片段</strong>
                  <span>{citations.length} 个片段</span>
                </div>
                <div className="harness-citation-group-list">
                  {citations.map((item, index) => (
                    <button
                      key={item.citationId}
                      type="button"
                      className={`harness-citation-group-item${item.citationId === citation.citationId ? ' is-active' : ''}`}
                      onClick={() => onSelectCitation?.(item)}
                    >
                      <span>{index + 1}</span>
                      <span>{item.pageNo != null ? `第 ${item.pageNo} 页` : '未提供页码'}</span>
                      {item.fileKind === 'crop' && <small>含引用图</small>}
                    </button>
                  ))}
                </div>
              </div>
            )}
            <div className="harness-citation-kind">
              <span className="harness-citation-kind-badge">{inlineFigure ? 'RAGFlow 内嵌裁切图' : sourceTypeLabel(citation.sourceType)}</span>
              <span>{inlineFigure ? '来自源文档的指定区域，不是源文件整页' : '源文档/记录引用'}</span>
            </div>
            <dl className="harness-citation-metadata">
              <div className="harness-citation-metadata-wide">
                <dt><strong>标题</strong><small>title</small></dt>
                <dd>{citation.title || '未命名来源'}</dd>
              </div>
              <div>
                <dt><strong>来源类型</strong><small>sourceType</small></dt>
                <dd>{sourceTypeLabel(citation.sourceType)}</dd>
              </div>
              <div>
                <dt><strong>外部文档 ID</strong><small>externalDocumentId</small></dt>
                <dd>{citation.externalDocumentId ?? '未提供'}</dd>
              </div>
              <div>
                <dt><strong>源版本 ID</strong><small>sourceVersionId</small></dt>
                <dd>{citation.sourceVersionId ?? '未提供'}</dd>
              </div>
              <div>
                <dt><strong>页面</strong><small>pageNo</small></dt>
                <dd>{citation.pageNo ?? '未提供'}</dd>
              </div>
              <div>
                <dt><strong>资产 ID</strong><small>assetId</small></dt>
                <dd>{citation.assetId ?? '未提供'}</dd>
              </div>
              {citation.recordId && (
                <div>
                  <dt><strong>记录 ID</strong><small>recordId</small></dt>
                  <dd>{citation.recordId}</dd>
                </div>
              )}
              {citation.refIndex != null && (
                <div>
                  <dt><strong>回答角标</strong><small>refIndex</small></dt>
                  <dd>{citation.refIndex}</dd>
                </div>
              )}
            </dl>
            <section className="harness-citation-section" aria-label="解析片段">
              <div className="harness-citation-section-head">
                <strong>解析片段</strong>
                <small>RAGFlow chunk excerpt</small>
              </div>
              <div className="harness-citation-markdown">
                <ReactMarkdown>{citation.excerpt ?? '未提供解析片段。'}</ReactMarkdown>
              </div>
            </section>
            {inlineFigure && citation.downloadUrl && (
              <figure className="harness-citation-figure">
                <img src={citation.downloadUrl} alt={`RAGFlow 内嵌裁切图：${citation.title}`} loading="lazy" referrerPolicy="no-referrer" />
                <figcaption>RAGFlow 内嵌裁切图（非源文件整页）</figcaption>
              </figure>
            )}
          </>
        )}
        {error && <p className="console-alert">[{error.code}] {error.message}</p>}
      </div>
    </section>
  );
}
