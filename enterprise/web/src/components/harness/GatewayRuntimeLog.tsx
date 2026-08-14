import { useCallback, useEffect, useState } from 'react';
import { Pause, Play, RefreshCw } from 'lucide-react';
import { toDisplayError, v2Api } from '../../api/v2Client';
import type { GatewayHttpLogEvent } from '../../api/consoleTypes';
import type { DisplayError } from '../../api/v2Types';

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleTimeString('zh-CN', { hour12: false });
}

function statusTone(status: number | null): string {
  if (status == null || status <= 0) return 'runtime-status--pending';
  if (status >= 500) return 'runtime-status--danger';
  if (status >= 400) return 'runtime-status--heat';
  return 'runtime-status--ok';
}

function preview(value: unknown): string {
  if (value == null) return '';
  try {
    const text = JSON.stringify(value);
    return text.length > 120 ? `${text.slice(0, 120)}…` : text;
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

export function GatewayRuntimeLog() {
  const [items, setItems] = useState<GatewayHttpLogEvent[]>([]);
  const [error, setError] = useState<DisplayError | null>(null);
  const [paused, setPaused] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const page = await v2Api.listHttpLog();
      setItems(page.items);
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

  return (
    <section aria-label="运行" data-testid="harness-runtime-log" className="console-card runtime-card">
      <div className="console-card-head">
        <div>
          <p className="console-eyebrow">Runtime</p>
          <h2>运行</h2>
          <p>网关收到的 HTTP 请求与回包。密钥、Token 已脱敏；完整正文不会落盘展示。</p>
        </div>
        <div className="console-card-actions">
          <span className={`runtime-live ${paused ? 'is-paused' : ''}`}>
            {paused ? '已暂停' : '实时'}
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
      <div className="console-card-body">
        {error && (
          <div role="alert" className="console-alert">
            <p>
              <strong>{error.code}</strong>
              {error.httpStatus ? ` · HTTP ${error.httpStatus}` : ''} · {error.message}
            </p>
          </div>
        )}
        {items.length === 0 && !error && (
          <p className="console-empty">暂无请求。向网关发 HTTP 后会出现在这里。</p>
        )}
        <ol className="runtime-log">
          {items.map((item) => {
            const path = item.query ? `${item.path}?${item.query}` : item.path;
            const open = openId === item.id;
            return (
              <li key={item.id}>
                <button
                  type="button"
                  className={`runtime-row ${open ? 'is-open' : ''}`}
                  onClick={() => setOpenId(open ? null : item.id)}
                >
                  <span className="runtime-time">{formatTime(item.ts)}</span>
                  <span className="runtime-dir">{item.direction === 'outbound' ? '发出' : '收到'}</span>
                  <span className="runtime-method">{item.method}</span>
                  <span className="runtime-path">{path}</span>
                  <span className={`runtime-status ${statusTone(item.http_status)}`}>
                    {item.http_status ?? '—'}
                  </span>
                  <span className="runtime-preview">{preview(item.body) || preview(item.response_body)}</span>
                </button>
                {open && (
                  <pre className="runtime-detail">
                    {dump({
                      kind: item.kind,
                      duration_ms: item.duration_ms,
                      streamed: item.streamed,
                      outcome: item.outcome,
                      error: item.error,
                      body: item.body,
                      response_body: item.response_body,
                    })}
                  </pre>
                )}
              </li>
            );
          })}
        </ol>
      </div>
    </section>
  );
}
