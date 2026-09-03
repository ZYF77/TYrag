import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { CalendarDays, Pause, Play, RefreshCw, RotateCcw, X } from 'lucide-react';
import { toDisplayError, v2Api } from '../../api/v2Client';
import type { GatewayHttpLogEvent } from '../../api/consoleTypes';
import type { DisplayError } from '../../api/v2Types';
import { ConsoleOverlay } from '../console/ConsoleOverlay';
import { DEFAULT_PAGE_SIZE, PaginationBar } from '../console/ConsoleTableControls';

type BusinessScene = 'feed' | 'inquiry' | 'callback' | 'admin' | 'other';
type InterfaceType = 'feed' | 'inquiry' | 'callback' | 'admin' | 'other';

interface RuntimeFilters {
  from: string;
  to: string;
  interfaceType: '' | InterfaceType;
  scene: '' | BusinessScene;
  issue: string;
  method: string;
  caller: string;
}

const EMPTY_FILTERS: RuntimeFilters = {
  from: '',
  to: '',
  interfaceType: '',
  scene: '',
  issue: '',
  method: '',
  caller: '',
};

const INTERFACE_LABELS: Record<InterfaceType, string> = {
  feed: '投喂接口',
  inquiry: '问数接口',
  callback: '结果回调',
  admin: '管理接口',
  other: '其他接口',
};

const SCENE_LABELS: Record<BusinessScene, string> = {
  feed: '投喂',
  inquiry: '问数',
  callback: '回调通知',
  admin: '系统管理',
  other: '其他',
};

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    });
}

function statusTone(status: number | null): string {
  if (status == null || status <= 0) return 'runtime-status--pending';
  if (status >= 500) return 'runtime-status--danger';
  if (status >= 400) return 'runtime-status--heat';
  return 'runtime-status--ok';
}

function preview(value: unknown, limit = 120): string {
  if (value == null) return '';
  try {
    const text = typeof value === 'string' ? value : JSON.stringify(value);
    return text.length > limit ? `${text.slice(0, limit)}…` : text;
  } catch {
    return String(value);
  }
}

function dump(value: unknown): string {
  if (value == null) return '无';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function isAdminPath(path: string): boolean {
  return path.startsWith('/enterprise/api/v1/admin/')
    || path === '/enterprise/api/v1/health'
    || path.startsWith('/enterprise/api/v1/auth/');
}

function isCallback(item: GatewayHttpLogEvent): boolean {
  return item.kind === 'feed.callback.outbound' || item.direction === 'outbound';
}

function businessScene(item: GatewayHttpLogEvent): BusinessScene {
  if (isCallback(item)) return 'callback';
  if (isAdminPath(item.path)) return 'admin';
  if (item.kind === 'feed.register.inbound' || item.path.startsWith('/enterprise/api/v3/documents')) return 'feed';
  if (item.kind === 'inquiry.http' || /\/(conversations|citations)(\/|$)/.test(item.path)) return 'inquiry';
  return 'other';
}

function interfaceType(item: GatewayHttpLogEvent): InterfaceType {
  if (isCallback(item)) return 'callback';
  if (isAdminPath(item.path)) return 'admin';
  if (item.kind === 'feed.register.inbound' || item.path.startsWith('/enterprise/api/v3/documents')) return 'feed';
  if (item.kind === 'inquiry.http') return 'inquiry';
  return 'other';
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function firstText(record: Record<string, unknown> | null, names: string[]): string {
  if (!record) return '';
  for (const name of names) {
    const value = record[name];
    if (typeof value === 'string' && value.trim()) return value.trim();
    if (typeof value === 'number') return String(value);
  }
  return '';
}

function failureDetail(item: GatewayHttpLogEvent): { code: string; reason: string } {
  const response = recordValue(item.response_body);
  const nested = recordValue(response?.error);
  const code = firstText(response, ['code', 'errorCode', 'faultCode'])
    || firstText(nested, ['code', 'errorCode', 'faultCode'])
    || (item.http_status && item.http_status >= 400 ? `HTTP_${item.http_status}` : '');
  const reason = item.error?.trim()
    || firstText(response, ['message', 'reason', 'detail', 'error'])
    || firstText(nested, ['message', 'reason', 'detail'])
    || (code ? '请求处理失败' : '');
  return { code, reason };
}

function callerName(item: GatewayHttpLogEvent): string {
  if (item.caller?.trim()) return item.caller.trim();
  const body = recordValue(item.body);
  return firstText(body, ['sourceSystem', 'source_system', 'caller', 'callerId']) || '未识别';
}

function requestorName(item: GatewayHttpLogEvent): string {
  const caller = callerName(item);
  const username = item.caller_username?.trim() || '';
  if (username && caller && caller !== username) return `${username} · ${caller}`;
  return username || caller;
}

function requestBodyText(item: GatewayHttpLogEvent): string {
  if (item.body != null) return dump(item.body);
  const method = item.method.toUpperCase();
  if (method === 'GET' || method === 'HEAD') return '无（GET/HEAD 请求无请求体）';
  return '未采集（日志规则未捕获）';
}

function filterTimestamp(value: string): number {
  const normalized = value.trim().replace(' ', 'T');
  return new Date(normalized).getTime();
}

function formatDateTimeLocal(value: string): string {
  if (!value) return '';
  return value.replace('T', ' ');
}

function DateRangeFilter({
  from,
  to,
  open,
  onOpen,
  triggerRef,
}: {
  from: string;
  to: string;
  open: boolean;
  onOpen: () => void;
  triggerRef: RefObject<HTMLButtonElement>;
}) {
  const label = from || to
    ? `${formatDateTimeLocal(from) || '不限'} — ${formatDateTimeLocal(to) || '不限'}`
    : '不限时间';
  return (
    <div className="runtime-datetime-field">
      <span>时间范围</span>
      <button
        ref={triggerRef}
        type="button"
        className="runtime-date-range-trigger"
        aria-label="时间范围"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={onOpen}
      >
        <CalendarDays size={15} aria-hidden="true" />
        <span>{label}</span>
      </button>
      <small>开始与结束时间</small>
    </div>
  );
}

function RuntimeDetail({
  item,
  caller,
  onClose,
}: {
  item: GatewayHttpLogEvent;
  caller: string;
  onClose: () => void;
}) {
  const failure = failureDetail(item);
  return (
    <ConsoleOverlay open mode="dialog" onClose={onClose} ariaLabel="HTTP 日志详情" className="runtime-detail-overlay">
      <section className="console-detail-dialog runtime-detail-dialog" aria-label="HTTP 日志详情">
        <header className="console-detail-dialog-head">
          <div>
            <p className="console-eyebrow">HTTP request</p>
            <h2>请求详情</h2>
            <p className="console-route">{item.method} {item.path}</p>
          </div>
          <button type="button" className="console-icon-button" aria-label="关闭 HTTP 日志详情" onClick={onClose}><X size={17} /></button>
        </header>
        <div className="console-detail-dialog-body">
          <div className="runtime-detail-grid">
            <section className="runtime-detail-block runtime-detail-block--request" data-testid="runtime-detail-request">
              <div className="runtime-detail-block-head">
                <strong>请求</strong>
                <span>{item.method}</span>
              </div>
              <dl>
                <div><dt>接口</dt><dd className="runtime-detail-mono">{item.path}</dd></div>
                {item.query && <div><dt>查询参数</dt><dd className="runtime-detail-mono">{item.query}</dd></div>}
                <div><dt>请求方</dt><dd>{caller}</dd></div>
              </dl>
              <div className="runtime-detail-payload-label">请求体</div>
              <pre>{requestBodyText(item)}</pre>
            </section>
            <section className="runtime-detail-block runtime-detail-block--response" data-testid="runtime-detail-response">
              <div className="runtime-detail-block-head">
                <strong>响应</strong>
                <span className={`runtime-status ${statusTone(item.http_status)}`}>{item.http_status ?? '—'}</span>
              </div>
              <dl>
                <div><dt>流式响应</dt><dd>{item.streamed ? '是' : '否'}</dd></div>
                <div><dt>处理结果</dt><dd>{item.outcome || '—'}</dd></div>
              </dl>
              <pre>{dump(item.response_body)}</pre>
            </section>
            <section className="runtime-detail-block runtime-detail-block--meta" data-testid="runtime-detail-meta">
              <div className="runtime-detail-block-head"><strong>处理信息</strong><span>{item.direction === 'outbound' ? 'Gateway 发出' : 'Gateway 收到'}</span></div>
              <dl>
                <div><dt>日志类型</dt><dd>{item.kind}</dd></div>
                <div><dt>耗时</dt><dd>{item.duration_ms == null ? '—' : `${item.duration_ms} ms`}</dd></div>
                <div><dt>失败码</dt><dd>{failure.code || '—'}</dd></div>
                <div><dt>失败原因</dt><dd>{failure.reason || '—'}</dd></div>
                <div><dt>错误信息</dt><dd>{item.error || '—'}</dd></div>
              </dl>
            </section>
          </div>
        </div>
      </section>
    </ConsoleOverlay>
  );
}

function isFailure(item: GatewayHttpLogEvent): boolean {
  return !item.http_status
    || item.http_status >= 400
    || Boolean(item.error)
    || ['failed', 'dead_letter'].includes(item.outcome ?? '');
}

function matchesFilters(item: GatewayHttpLogEvent, filters: RuntimeFilters): boolean {
  const timestamp = new Date(item.ts).getTime();
  const from = filters.from ? filterTimestamp(filters.from) : Number.NEGATIVE_INFINITY;
  const to = filters.to ? filterTimestamp(filters.to) : Number.POSITIVE_INFINITY;
  if (Number.isFinite(timestamp) && (timestamp < from || timestamp > to)) return false;
  if (filters.interfaceType && interfaceType(item) !== filters.interfaceType) return false;
  if (filters.scene && businessScene(item) !== filters.scene) return false;
  if (filters.method && item.method.toUpperCase() !== filters.method) return false;
   if (filters.caller && !requestorName(item).toLowerCase().includes(filters.caller.trim().toLowerCase())) return false;
  if (filters.issue) {
    const failure = failureDetail(item);
    const haystack = [failure.code, failure.reason, item.error, preview(item.body, 2000), preview(item.response_body, 2000)]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
    if (!haystack.includes(filters.issue.trim().toLowerCase())) return false;
  }
  return true;
}

export function GatewayRuntimeLog() {
  const [items, setItems] = useState<GatewayHttpLogEvent[]>([]);
  const [error, setError] = useState<DisplayError | null>(null);
  const [paused, setPaused] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [dateRangeOpen, setDateRangeOpen] = useState(false);
  const [draftRange, setDraftRange] = useState({ from: '', to: '' });
  const dateRangeTriggerRef = useRef<HTMLButtonElement>(null);
  const [filters, setFilters] = useState<RuntimeFilters>(EMPTY_FILTERS);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  const load = useCallback(async () => {
    try {
      const result = await v2Api.listHttpLog(200);
      setItems(result.items);
      setError(null);
    } catch (reason) {
      setError(toDisplayError(reason));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (paused) return undefined;
    const timer = window.setInterval(() => {
      void load();
    }, 2000);
    return () => window.clearInterval(timer);
  }, [load, paused]);

  const filteredItems = useMemo(
    () => items
      .filter((item) => matchesFilters(item, filters))
      .sort((left, right) => new Date(right.ts).getTime() - new Date(left.ts).getTime()),
    [filters, items],
  );
  const pageCount = Math.max(1, Math.ceil(filteredItems.length / pageSize));
  const pageItems = filteredItems.slice((page - 1) * pageSize, page * pageSize);
  const failures = filteredItems.filter(isFailure).length;
  const successes = filteredItems.filter((item) => !isFailure(item)).length;
  const durations = filteredItems.flatMap((item) => item.duration_ms == null ? [] : [item.duration_ms]);
  const averageDuration = durations.length
    ? Math.round(durations.reduce((sum, duration) => sum + duration, 0) / durations.length)
    : 0;
  const failureRate = filteredItems.length ? Math.round((failures / filteredItems.length) * 1000) / 10 : 0;

  useEffect(() => {
    if (page > pageCount) setPage(pageCount);
  }, [page, pageCount]);

  const updateFilter = <K extends keyof RuntimeFilters>(name: K, value: RuntimeFilters[K]) => {
    setFilters((current) => ({ ...current, [name]: value }));
    setPage(1);
  };

  const openDateRange = () => {
    setDraftRange({ from: filters.from, to: filters.to });
    setDateRangeOpen(true);
  };

  const applyDateRange = () => {
    setFilters((current) => ({ ...current, from: draftRange.from, to: draftRange.to }));
    setPage(1);
    setDateRangeOpen(false);
  };

  const selectedItem = openId ? items.find((item) => item.id === openId) ?? null : null;

  return (
    <section aria-label="运行" data-testid="harness-runtime-log" className="console-card runtime-card">
      <div className="console-card-head">
        <div>
          <p className="console-eyebrow">Runtime observatory</p>
          <h2>运行日志</h2>
          <p>最近 200 条 Gateway HTTP 请求。正文按安全规则截断并脱敏，默认按时间倒序。</p>
        </div>
        <div className="console-card-actions">
          <span className={`runtime-live ${paused ? 'is-paused' : ''}`}>
            {paused ? '已暂停' : '每 2 秒刷新'}
          </span>
          <button
            type="button"
            onClick={() => setPaused((current) => !current)}
            className="console-secondary-button"
          >
            {paused ? <Play size={14} /> : <Pause size={14} />}
            {paused ? '继续刷新' : '暂停刷新'}
          </button>
          <button type="button" onClick={() => void load()} className="console-icon-button" aria-label="刷新运行日志">
            <RefreshCw size={16} />
          </button>
        </div>
      </div>

      <div className="runtime-overview" aria-label="当前筛选请求汇总">
        <div className="runtime-overview-lead">
          <p>当前筛选</p>
          <strong data-testid="runtime-total">{filteredItems.length}</strong>
          <span>条请求</span>
          <div className="runtime-health-track" aria-hidden="true">
            <span style={{ width: `${filteredItems.length ? (successes / filteredItems.length) * 100 : 0}%` }} />
            <i style={{ width: `${failureRate}%` }} />
          </div>
        </div>
        <dl className="runtime-overview-stats">
          <div><dt>成功</dt><dd>{successes}</dd></div>
          <div><dt>异常</dt><dd data-testid="runtime-failures" className={failures ? 'is-danger' : ''}>{failures}</dd></div>
          <div><dt>异常率</dt><dd>{failureRate}%</dd></div>
          <div><dt>平均耗时</dt><dd>{averageDuration}<small> ms</small></dd></div>
        </dl>
      </div>

      <div className="runtime-filter-panel" aria-label="运行日志筛选">
        <div className="runtime-filter-grid">
          <DateRangeFilter from={filters.from} to={filters.to} open={dateRangeOpen} onOpen={openDateRange} triggerRef={dateRangeTriggerRef} />
          <label>
            <span>接口类型</span>
            <select aria-label="接口类型" value={filters.interfaceType} onChange={(event) => updateFilter('interfaceType', event.target.value as RuntimeFilters['interfaceType'])}>
              <option value="">全部接口</option>
              <option value="feed">投喂接口</option>
              <option value="inquiry">问数接口</option>
              <option value="callback">结果回调</option>
              <option value="admin">管理接口</option>
              <option value="other">其他接口</option>
            </select>
          </label>
          <label>
            <span>业务场景</span>
            <select aria-label="业务场景" value={filters.scene} onChange={(event) => updateFilter('scene', event.target.value as RuntimeFilters['scene'])}>
              <option value="">全部场景</option>
              <option value="feed">投喂</option>
              <option value="inquiry">问数</option>
              <option value="callback">回调通知</option>
              <option value="admin">系统管理</option>
              <option value="other">其他</option>
            </select>
          </label>
          <label>
            <span>HTTP 方法</span>
            <select aria-label="HTTP 方法" value={filters.method} onChange={(event) => updateFilter('method', event.target.value)}>
              <option value="">全部方法</option>
              {['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((method) => <option key={method} value={method}>{method}</option>)}
            </select>
          </label>
          <label>
            <span>失败原因 / 故障码</span>
            <input aria-label="失败原因或故障码" type="search" value={filters.issue} onChange={(event) => updateFilter('issue', event.target.value)} placeholder="如 AUTH_TOKEN_INVALID" />
          </label>
          <label>
            <span>请求方 / 调用方 / 用户名</span>
            <input aria-label="请求方、调用方或用户名" type="search" value={filters.caller} onChange={(event) => updateFilter('caller', event.target.value)} placeholder="用户名、sourceSystem、Key ID 或地址" />
          </label>
          <button type="button" className="runtime-reset" onClick={() => { setFilters(EMPTY_FILTERS); setPage(1); }}>
            <RotateCcw size={14} />
            重置筛选
          </button>
        </div>
      </div>

      <div className="console-card-body">
        {error && (
          <div role="alert" className="console-alert">
            <p>
              <strong>{error.code}</strong>
              {error.httpStatus ? ` · HTTP ${error.httpStatus}` : ''} · {error.message}
            </p>
          </div>
        )}
        {filteredItems.length === 0 && !error && (
          <p className="console-empty">当前筛选下没有请求。调整条件，或向 Gateway 发起一次调用。</p>
        )}
        {filteredItems.length > 0 && (
          <>
            <div className="runtime-table-head" aria-hidden="true">
              <span>时间</span><span>场景 / 类型</span><span>方法</span><span>接口与结果</span><span>请求方</span><span>状态</span><span>耗时</span>
            </div>
            <ol className="runtime-log">
              {pageItems.map((item) => {
                const path = item.query ? `${item.path}?${item.query}` : item.path;
                const open = openId === item.id;
                const scene = businessScene(item);
                const kind = interfaceType(item);
                const failure = failureDetail(item);
                const caller = requestorName(item);
                return (
                  <li key={item.id}>
                    <button
                      type="button"
                      className={`runtime-row ${open ? 'is-open' : ''}`}
                      onClick={() => setOpenId(open ? null : item.id)}
                      aria-expanded={open}
                    >
                      <time className="runtime-time" dateTime={item.ts}>{formatTime(item.ts)}</time>
                      <span className="runtime-kind"><b>{SCENE_LABELS[scene]}</b><small>{INTERFACE_LABELS[kind]}</small></span>
                      <span className="runtime-method">{item.method}</span>
                      <span className="runtime-main"><b>{path}</b><small>{failure.code || failure.reason || preview(item.response_body) || preview(item.body) || '请求已完成'}</small></span>
                      <span className="runtime-caller">{caller}</span>
                      <span className={`runtime-status ${statusTone(item.http_status)}`}>{item.http_status ?? '—'}</span>
                      <span className="runtime-duration">{item.duration_ms == null ? '—' : `${item.duration_ms} ms`}</span>
                    </button>
                  </li>
                );
              })}
            </ol>
            <PaginationBar
              page={page}
              itemCount={pageItems.length}
              total={filteredItems.length}
              hasMore={page < pageCount}
              pageSize={pageSize}
              onPageSizeChange={(value) => { setPageSize(value); setPage(1); }}
              onPrevious={() => setPage((current) => Math.max(1, current - 1))}
              onNext={() => setPage((current) => Math.min(pageCount, current + 1))}
            />
          </>
        )}
      </div>
      <ConsoleOverlay
        open={dateRangeOpen}
        mode="popover"
        anchorRef={dateRangeTriggerRef}
        onClose={() => setDateRangeOpen(false)}
        ariaLabel="时间范围"
        className="runtime-date-range-popover"
      >
        <div className="runtime-date-range-head">
          <div>
            <strong>时间范围</strong>
            <span>使用本地时间筛选请求</span>
          </div>
          <button type="button" className="console-icon-button" aria-label="关闭时间范围" onClick={() => setDateRangeOpen(false)}><X size={16} /></button>
        </div>
        <div className="runtime-date-range-fields">
          <label>
            <span>开始时间</span>
            <input
              aria-label="开始时间"
              type="datetime-local"
              value={draftRange.from}
              onChange={(event) => setDraftRange((current) => ({ ...current, from: event.target.value }))}
            />
          </label>
          <label>
            <span>结束时间</span>
            <input
              aria-label="结束时间"
              type="datetime-local"
              value={draftRange.to}
              onChange={(event) => setDraftRange((current) => ({ ...current, to: event.target.value }))}
            />
          </label>
        </div>
        {draftRange.from && draftRange.to && filterTimestamp(draftRange.from) > filterTimestamp(draftRange.to) && (
          <p className="console-alert">结束时间必须晚于开始时间。</p>
        )}
        <div className="runtime-date-range-actions">
          <button type="button" className="console-secondary-button" onClick={() => { setDraftRange({ from: '', to: '' }); setFilters((current) => ({ ...current, from: '', to: '' })); setPage(1); setDateRangeOpen(false); }}>清除</button>
          <button
            type="button"
            className="console-primary-button"
            disabled={Boolean(draftRange.from && draftRange.to && filterTimestamp(draftRange.from) > filterTimestamp(draftRange.to))}
            onClick={applyDateRange}
          >应用筛选</button>
        </div>
      </ConsoleOverlay>
      {selectedItem && <RuntimeDetail item={selectedItem} caller={requestorName(selectedItem)} onClose={() => setOpenId(null)} />}
    </section>
  );
}
