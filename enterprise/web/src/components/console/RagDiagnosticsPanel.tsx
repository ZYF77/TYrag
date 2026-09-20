import { useCallback, useEffect, useState } from 'react';
import { ChevronRight, RefreshCw, X } from 'lucide-react';
import { toDisplayError, v2Api } from '../../api/v2Client';
import type {
  ConsoleState,
  RagDiagnosticTraceDetail,
  RagDiagnosticTracePage,
} from '../../api/consoleTypes';
import { PanelCard, PanelError, panelErrorStatus } from '../common/Panel';
import { ConsoleDataTable } from '../common/ConsoleDataTable';
import { DialogHeader } from '../common/DialogHeader';
import { formatTime } from '../../lib/format';
import { DEFAULT_PAGE_SIZE, PaginationBar } from './ConsoleTableControls';
import { ConsoleOverlay } from './ConsoleOverlay';

function initialState<T>(): ConsoleState<T> {
  return { status: 'processing', data: null, error: null };
}

const STAGE_LABELS: Record<string, string> = {
  answer_generation: '最终回答生成',
  candidate_search: '候选检索',
  context_build: 'Context 组装',
  cross_languages: '跨语言查询改写',
  embedding: '查询向量生成',
  gateway_request: 'Gateway 请求',
  gateway_scope: 'Gateway 范围/权限',
  knowledge_graph: '知识图谱检索',
  keyword_analysis: '关键词分析',
  metadata_filter: '元数据过滤',
  model_bind: '模型绑定',
  ragflow_request: 'RAGFlow 请求',
  reference_metadata: '引用元数据补充',
  refine_multiturn: '多轮问题改写',
  rerank: 'Rerank 重排序',
  sql_generation: 'SQL 查询',
  stream_first_token: '首个流式输出',
  stream_first_reasoning: '首个思考输出',
  stream_first_answer: '首段正文输出',
  run_started: 'run.started 发送',
  gateway_prepare: 'Gateway 请求准备',
  http_response: 'HTTP 首响应',
  toc_enhance: '目录增强',
  web_search: '联网检索',
};

const EVENT_LABELS: Record<string, string> = {
  workflow_node_started: '节点执行开始',
  workflow_node_finished: '节点执行结束',
  workflow_node_stream_started: '节点流式生成开始',
  workflow_node_stream_finished: '节点流式生成结束',
  workflow_tool_started: '工具调用开始',
  workflow_tool_finished: '工具调用结束',
  workflow_error: '工作流执行异常',
  wf_node_started: 'Canvas 节点开始',
  wf_node_finished: 'Canvas 节点结束',
  wf_tool_call: '工具调用摘要',
  context: 'Context',
  llm: 'LLM',
  outcome: '结果',
  request: '请求',
  retrieval: '检索汇总',
  scope: '范围/权限',
};

function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

function eventDurationMs(event: RagDiagnosticsPanelEvent): number | null {
  const topLevel = numberValue(event.durationMs);
  if (topLevel !== null) return topLevel;
  return numberValue(event.data.durationMs);
}

function eventAtMs(events: RagDiagnosticTraceDetail['diagnostics']['events'], type: string): number | null {
  const event = events.find((item) => item.type === type);
  return event ? numberValue(event.atMs) : null;
}

function eventDurationByType(events: RagDiagnosticTraceDetail['diagnostics']['events'], type: string): number | null {
  const event = events.find((item) => item.type === type);
  return event ? eventDurationMs(event) : null;
}

function eventElapsedFromRequest(events: RagDiagnosticTraceDetail['diagnostics']['events'], type: string): number | null {
  const atMs = eventAtMs(events, type);
  if (atMs === null) return null;
  return atMs + (eventDurationByType(events, 'gateway_prepare') ?? 0);
}

function eventDurationByStage(events: RagDiagnosticTraceDetail['diagnostics']['events'], stage: string): number | null {
  const event = events.find((item) => item.data.stage === stage);
  return event ? eventDurationMs(event) : null;
}

function eventLabel(event: RagDiagnosticsPanelEvent): string {
  if (event.type.startsWith('workflow_') || event.type.startsWith('wf_')) {
    const name = event.data.toolName || event.data.componentName || event.data.componentType;
    return `${EVENT_LABELS[event.type] || event.type}${typeof name === 'string' ? ` · ${name}` : ''}`;
  }
  const stage = typeof event.data.stage === 'string' ? event.data.stage : '';
  const label = STAGE_LABELS[stage] || stage;
  if (event.type === 'llm' && label) return `LLM · ${label}`;
  if (label) return label;
  if (event.type === 'llm' && typeof event.data.callType === 'string') {
    return `LLM · ${event.data.callType}`;
  }
  return EVENT_LABELS[event.type] || event.type;
}

type RagDiagnosticsPanelEvent = RagDiagnosticTraceDetail['diagnostics']['events'][number];

const COMPONENT_PURPOSES: Record<string, string> = {
  Begin: '接收工作流输入', Agent: '模型推理与工具调度', LLM: '模型生成',
  Retrieval: '知识检索', Message: '输出回答', Categorize: '分类路由',
  Switch: '条件分支', Iteration: '迭代执行', Loop: '循环执行',
  Code: '代码执行', Invoke: '外部请求',
};

function eventSummary(event: RagDiagnosticsPanelEvent): string {
  const data = event.data;
  const type = typeof data.componentType === 'string' ? data.componentType : '';
  return [
    type && `${type}${COMPONENT_PURPOSES[type] ? `（${COMPONENT_PURPOSES[type]}）` : ''}`,
    data.componentId && `节点 ${data.componentId}`,
    data.modelId && `模型 ${data.modelId}`,
    data.toolType && `工具类型 ${data.toolType}`,
    data.status && `状态 ${data.status}`,
    data.spanId && `执行 ${data.spanId}`,
    data.parentSpanId && `父执行 ${data.parentSpanId}`,
    data.deferred && '流式生成另行计时',
  ].filter(Boolean).join(' · ');
}

function jsonPrimitive(value: unknown): { text: string; tone: string } {
  if (value === null) return { text: 'null', tone: 'null' };
  if (typeof value === 'string') return { text: JSON.stringify(value), tone: 'string' };
  if (typeof value === 'number' || typeof value === 'boolean') return { text: String(value), tone: typeof value };
  return { text: String(value), tone: 'unknown' };
}

const JSON_KEY_LABELS: Record<string, string> = {
  componentId: '节点 ID', componentName: '节点名称', componentType: '节点类型',
  spanId: '执行 ID', parentSpanId: '父执行 ID', spanKind: '执行类别',
  toolName: '工具名称', toolKind: '工具来源', errorType: '错误类别',
  query: '规范化查询（RAGFlow 实际检索文本）',
  reasoningMode: '推理模式',
  similarityThreshold: '相似度阈值',
  topN: '召回数量',
  durationMs: '耗时',
  requestedDocumentIds: '请求文档',
  allowedDocumentIds: '允许文档',
};

function jsonKeyLabel(label: string): string {
  return JSON_KEY_LABELS[label] ?? label;
}

function JsonTreeNode({ label, value, depth }: { label: string; value: unknown; depth: number }) {
  const isBranch = value !== null && typeof value === 'object';
  const entries = isBranch
    ? Array.isArray(value)
      ? value.map((item, index) => [String(index), item] as const)
      : Object.entries(value)
    : [];
  const [expanded, setExpanded] = useState(false);

  if (!isBranch) {
    const primitive = jsonPrimitive(value);
    return (
      <div className="rag-json-leaf">
        <span className="rag-json-key">{jsonKeyLabel(label)}{jsonKeyLabel(label) !== label ? ` · ${label}` : ''}</span>
        <span className={`rag-json-value rag-json-value--${primitive.tone}`}>{primitive.text}</span>
      </div>
    );
  }

  const kind = Array.isArray(value) ? '数组' : '对象';
  const summary = entries.length === 0 ? '空' : `${entries.length} 项`;
  return (
    <div className="rag-json-node">
      <button
        type="button"
        className="rag-json-toggle"
        onClick={() => setExpanded((current) => !current)}
        aria-expanded={expanded}
        disabled={entries.length === 0}
      >
        <ChevronRight size={14} aria-hidden="true" className={expanded ? 'is-expanded' : undefined} />
        <span className="rag-json-key">{jsonKeyLabel(label)}{jsonKeyLabel(label) !== label ? ` · ${label}` : ''}</span>
        <span className="rag-json-kind">{kind} · {summary}</span>
      </button>
      {expanded && entries.length > 0 && (
        <div className="rag-json-children">
          {entries.map(([childLabel, childValue]) => (
            <JsonTreeNode key={`${label}-${childLabel}`} label={childLabel} value={childValue} depth={depth + 1} />
          ))}
        </div>
      )}
    </div>
  );
}

function JsonTree({ value }: { value: unknown }) {
  return (
    <div className="rag-json-tree" aria-label="格式化诊断数据">
      <JsonTreeNode label="数据" value={value} depth={0} />
    </div>
  );
}

export function RagDiagnosticsPanel() {
  const [list, setList] = useState<ConsoleState<RagDiagnosticTracePage>>(initialState);
  const [detail, setDetail] = useState<ConsoleState<RagDiagnosticTraceDetail>>({
    status: 'healthy',
    data: null,
    error: null,
  });
  const [runId, setRunId] = useState('');
  const [detailOpen, setDetailOpen] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  const loadList = useCallback(async () => {
    setList(initialState());
    try {
      const data = await v2Api.listRagDiagnosticTraces(pageSize, (page - 1) * pageSize);
      setList({ status: 'healthy', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setList({ status: panelErrorStatus(displayError), data: null, error: displayError });
    }
  }, [page, pageSize]);

  const loadDetail = useCallback(async (id: string) => {
    const value = id.trim();
    if (!value) return;
    setRunId(value);
    setDetailOpen(true);
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
    <>
      <PanelCard
        eyebrow="RAG diagnostics"
        title="RAG 诊断"
        description="按 runId 查看检索范围、候选排序、Context 取舍、LLM 时序与失败点；详情在弹出面板中查看。"
        status={list.status}
        className="console-table-card"
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
          <ConsoleDataTable
            columns={[
              { key: 'runId', label: 'runId', render: (item) => <td key="runId" className="console-table-mono">{item.runId}</td> },
              { key: 'outcome', label: '结果', render: (item) => <td key="outcome">{item.outcome ?? item.status}{item.truncated ? ' · truncated' : ''}</td> },
              { key: 'durationMs', label: '耗时', render: (item) => <td key="durationMs">{item.durationMs == null ? '未计时' : `${Math.round(item.durationMs)}ms`}</td> },
              { key: 'createdAt', label: '时间', render: (item) => <td key="createdAt">{formatTime(item.createdAt)}</td> },
              {
                key: 'actions',
                label: '操作',
                render: (item) => (
                  <td key="actions" className="console-col-center console-col-actions"><button type="button" className="console-secondary-button" onClick={(event) => { event.stopPropagation(); void loadDetail(item.runId); }}>查看</button></td>
                ),
              },
            ]}
            items={list.data.items}
            rowKey={(item) => item.runId}
            onRowAction={(item) => void loadDetail(item.runId)}
            emptyRow={<tr><td colSpan={5}>暂无诊断记录。功能默认关闭，仅显示开启后产生的运行。</td></tr>}
            pagination={(
              <PaginationBar
                page={page}
                itemCount={list.data.items.length}
                hasMore={list.data.hasMore}
                pageSize={pageSize}
                onPageSizeChange={(value) => { setPageSize(value); setPage(1); }}
                onPrevious={() => setPage((current) => Math.max(1, current - 1))}
                onNext={() => setPage((current) => current + 1)}
              />
            )}
          />
        )}
      </PanelCard>

      <ConsoleOverlay
        open={detailOpen}
        mode="dialog"
        onClose={() => setDetailOpen(false)}
        ariaLabel="诊断运行详情"
        className="rag-diagnostics-detail-overlay"
      >
          <section className="rag-diagnostics-detail-modal" aria-labelledby="rag-diagnostics-detail-title">
            <DialogHeader
              headClassName="rag-diagnostics-detail-head"
              eyebrow="RAG diagnostics · run detail"
              title="诊断运行详情"
              titleId="rag-diagnostics-detail-title"
              meta={<p className="rag-diagnostics-detail-run-id">{runId}</p>}
              closeLabel="关闭诊断详情"
              onClose={() => setDetailOpen(false)}
              closeContent={<X size={17} />}
            />

            <div className="rag-diagnostics-detail-body">
              <div className="rag-diagnostics-detail-summary">
                <div><span>状态</span><strong>{detail.data?.status ?? (detail.status === 'processing' ? '加载中' : detail.status)}</strong></div>
                <div><span>总耗时</span><strong>{detail.data?.diagnostics.durationMs == null ? '未计时' : `${Math.round(detail.data.diagnostics.durationMs)}ms`}</strong></div>
                <div><span>记录时间</span><strong>{detail.data ? formatTime(detail.data.createdAt) : '—'}</strong></div>
              </div>
              {detail.data && (
                <div className="rag-diagnostics-timing-summary">
                  <div><span>run.started</span><strong>{eventDurationByType(detail.data.diagnostics.events, 'run_started') == null ? '未记录' : String(Math.round(eventDurationByType(detail.data.diagnostics.events, 'run_started')!)) + 'ms'}</strong></div>
                  <div><span>首个思考</span><strong>{eventElapsedFromRequest(detail.data.diagnostics.events, 'stream_first_reasoning') == null ? '未记录' : String(Math.round(eventElapsedFromRequest(detail.data.diagnostics.events, 'stream_first_reasoning')!)) + 'ms'}</strong></div>
                  <div><span>首段正文</span><strong>{eventElapsedFromRequest(detail.data.diagnostics.events, 'stream_first_answer') == null ? '未记录' : String(Math.round(eventElapsedFromRequest(detail.data.diagnostics.events, 'stream_first_answer')!)) + 'ms'}</strong></div>
                  <div><span>正文输出阶段</span><strong>{eventDurationByStage(detail.data.diagnostics.events, 'answer_generation') == null ? '未记录' : String(Math.round(eventDurationByStage(detail.data.diagnostics.events, 'answer_generation')!)) + 'ms'}</strong></div>
                </div>
              )}

              {detail.error && <PanelError error={detail.error} onRetry={() => void loadDetail(runId)} />}
              {detail.status === 'processing' && <p className="console-empty">正在读取诊断记录…</p>}
              {detail.data && (
                <>
                  <p className="console-help-text rag-diagnostics-detail-help">
                    有明确 durationMs 的阶段显示本步骤耗时；request、scope、context、outcome 等是阶段节点，只显示累计时间。诊断数据默认收起，点击对象或数组节点继续查看下一层。
                  </p>
                  {detail.data.diagnostics.truncated && <p role="status">诊断已达到采集上限，以下不是完整执行记录。</p>}
                  <div className="rag-diagnostics-event-list">
                    {detail.data.diagnostics.events.map((event, index) => {
                      const duration = eventDurationMs(event);
                      return (
                        <article key={`${event.type}-${index}`} className="rag-diagnostics-event">
                          <header className="rag-diagnostics-event-head">
                            <div className="rag-diagnostics-event-title">
                              <span className="rag-diagnostics-event-index">{String(index + 1).padStart(2, '0')}</span>
                              <div>
                                <h3>{eventLabel(event)}</h3>
                                <p>{event.type}</p>
                                {eventSummary(event) && <p>{eventSummary(event)}</p>}
                              </div>
                            </div>
                            <div className="rag-diagnostics-event-timing">
                              <span>{duration === null ? '阶段节点' : '本步骤'} <strong>{duration === null ? '未单独计时' : `${Math.round(duration)}ms`}</strong></span>
                              <span>累计 <strong>{Math.round(event.atMs)}ms</strong></span>
                            </div>
                          </header>
                          <JsonTree value={event.data} />
                        </article>
                      );
                    })}
                  </div>
                </>
              )}
            </div>
          </section>
      </ConsoleOverlay>
    </>
  );
}
