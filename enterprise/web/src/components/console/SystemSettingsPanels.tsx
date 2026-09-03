import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, ArrowUpDown, Columns3, RefreshCw, SlidersHorizontal } from 'lucide-react';
import { toDisplayError, v2Api } from '../../api/v2Client';
import { ConsoleOverlay } from './ConsoleOverlay';
import { DEFAULT_PAGE_SIZE, PaginationBar } from './ConsoleTableControls';
import { DocumentInspector } from './DocumentInspector';
import { ConversationInspector } from './ConversationInspector';
import type {
  CallbackEndpointConfig,
  ConsoleModuleStatus,
  ConsoleState,
  ConversationMetadataFilters,
  ConversationMetadataItem,
  ConversationMetadataOrderBy,
  ConversationMetadataPage,
  DocumentMetadataFilters,
  DocumentMetadataItem,
  DocumentMetadataOrderBy,
  DocumentMetadataPage,
  EamProbeResult,
  GatewayRuntimeSettings,
  MetadataSortOrder,
  MetadataSummary,
  SystemIntegrations,
} from '../../api/consoleTypes';
import type { DisplayError } from '../../api/v2Types';

const PAGE_LIMIT = DEFAULT_PAGE_SIZE;
const NOT_PROVIDED = '未提供';

const CONVERSATION_STATUS_OPTIONS = ['active', 'archived'] as const;
// 常见同步状态，对齐 enterprise/gateway/sync/status_mapping.py 的 stage 枚举。
const SYNC_STATUS_OPTIONS = [
  'ready',
  'failed',
  'parsing',
  'registered',
  'cancelled',
  'queued',
  'indexing',
  'superseded',
] as const;
const BUSINESS_STATUS_OPTIONS = ['active', 'review_required'] as const;

const DOC_HIDDEN_COLUMNS_KEY = 'console.docMeta.hiddenColumns';
const DOC_DEFAULT_HIDDEN_COLUMNS = ['sourceSize', 'createdAt'];

type DocumentFilterKey = keyof Required<DocumentMetadataFilters>;
type DocumentFilterDraft = Record<DocumentFilterKey, string>;
const EMPTY_DOCUMENT_FILTERS: DocumentFilterDraft = {
  externalDocumentId: '',
  sourceVersionId: '',
  fileName: '',
  equipmentId: '',
  fixedAssetNo: '',
  assetId: '',
  ragflowDocumentId: '',
};
const DOCUMENT_ADVANCED_FIELDS: Array<{ key: DocumentFilterKey; label: string; placeholder: string }> = [
  { key: 'externalDocumentId', label: '外部文档 ID', placeholder: '输入 externalDocumentId' },
  { key: 'sourceVersionId', label: '来源版本', placeholder: '输入 sourceVersionId' },
  { key: 'fileName', label: '文件名', placeholder: '输入完整文件名' },
  { key: 'equipmentId', label: '设备编号', placeholder: '输入设备编号' },
  { key: 'fixedAssetNo', label: '固定资产编号', placeholder: '输入固定资产编号' },
  { key: 'assetId', label: '资产 ID', placeholder: '输入 assetId' },
  { key: 'ragflowDocumentId', label: 'RAGFlow 文档 ID', placeholder: '输入 RAGFlow document ID' },
];

function normaliseDocumentFilters(filters: DocumentFilterDraft): DocumentFilterDraft {
  return Object.fromEntries(Object.entries(filters).map(([key, value]) => [key, value.trim()])) as DocumentFilterDraft;
}

function hasDocumentFilters(filters: DocumentFilterDraft): boolean {
  return Object.values(filters).some((value) => value.trim() !== '');
}

interface ProbeState {
  phase: 'idle' | 'probing' | 'connected' | 'failed';
  result?: EamProbeResult;
  error?: DisplayError;
}

function initialPanelState<T>(): ConsoleState<T> {
  return { status: 'processing', data: null, error: null };
}

export function panelErrorStatus(error: DisplayError): ConsoleModuleStatus {
  if (error.httpStatus === 401 || error.httpStatus === 403) return 'unauthorized';
  if (error.httpStatus === 0 || error.httpStatus === 502 || error.httpStatus === 503) {
    return 'unavailable';
  }
  return 'failed';
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return NOT_PROVIDED;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

function formatValue(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return NOT_PROVIDED;
  return String(value);
}

function formatMiB(value: number | null | undefined): string {
  if (value === null || value === undefined) return NOT_PROVIDED;
  const mib = value / (1024 * 1024);
  return `${Number.isInteger(mib) ? mib : mib.toFixed(2)} MiB`;
}

function PanelBadge({ status }: { status: ConsoleModuleStatus }) {
  return (
    <span className={`console-status console-status--${status}`}>
      <span className="console-status-dot" aria-hidden="true" />
      {status}
    </span>
  );
}

export function PanelCard({
  eyebrow,
  title,
  description,
  status,
  actions,
  children,
  testId,
  className,
}: {
  eyebrow: string;
  title: string;
  description: string;
  status: ConsoleModuleStatus;
  actions?: React.ReactNode;
  children: React.ReactNode;
  testId: string;
  className?: string;
}) {
  return (
    <section data-testid={testId} className={`console-card${className ? ` ${className}` : ''}`}>
      <div className="console-card-head">
        <div>
          <p className="console-eyebrow">{eyebrow}</p>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
        <div className="console-card-actions">
          <PanelBadge status={status} />
          {actions}
        </div>
      </div>
      <div className="console-card-body">{children}</div>
    </section>
  );
}

export function PanelError({ error, onRetry }: { error: DisplayError; onRetry: () => void }) {
  return (
    <div role="alert" className="console-alert">
      <p><strong>{error.code}</strong>{error.httpStatus ? ` · HTTP ${error.httpStatus}` : ''} · {error.message}</p>
      <button type="button" onClick={onRetry} className="console-secondary-button">重试</button>
    </div>
  );
}

function ProbeBadge({ state }: { state: ProbeState }) {
  if (state.phase === 'probing') {
    return (
      <span className="console-status console-status--processing">
        <span className="console-status-dot" aria-hidden="true" />
        检测中
      </span>
    );
  }
  if (state.phase === 'connected') {
    return (
      <span className="console-status console-status--connected">
        <span className="console-status-dot" aria-hidden="true" />
        connected
        {state.result?.httpStatus != null ? ` · HTTP ${state.result.httpStatus}` : ''}
        {state.result?.latencyMs != null ? ` · ${state.result.latencyMs}ms` : ''}
      </span>
    );
  }
  if (state.phase === 'failed') {
    const detail = state.result?.errorCode ?? state.error?.code;
    return (
      <span className="console-status console-status--failed">
        <span className="console-status-dot" aria-hidden="true" />
        failed
        {state.result?.httpStatus != null ? ` · HTTP ${state.result.httpStatus}` : ''}
        {detail ? ` · ${detail}` : ''}
      </span>
    );
  }
  return null;
}

type IntegrationSection = 'ragflow' | 'runtime' | 'callbacks';

function RuntimeEffectBadge({
  restart = false,
  restartLabel = '只读',
}: {
  restart?: boolean;
  restartLabel?: string;
}) {
  const readOnly = restart && restartLabel === '只读';
  return (
    <span className={`runtime-effect-badge ${readOnly ? 'runtime-effect-badge--readonly' : restart ? 'runtime-effect-badge--restart' : 'runtime-effect-badge--hot'}`}>
      {restart ? restartLabel : '可热加载'}
    </span>
  );
}

function RuntimeSwitch({
  checked,
  label,
  onChange,
}: {
  checked: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className={`runtime-toggle ${checked ? 'is-on' : ''}`}>
      <input
        className="runtime-toggle-input"
        type="checkbox"
        checked={checked}
        aria-label={label}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="runtime-toggle-track" aria-hidden="true"><span /></span>
      <span className="runtime-toggle-text">{checked ? '已启用' : '已停用'}</span>
    </label>
  );
}

function RuntimeRangeField({
  label,
  value,
  min,
  max,
  step = 1,
  unit,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  unit: string;
  onChange: (value: number) => void;
}) {
  const progress = max === min
    ? 0
    : Math.max(0, Math.min(100, ((Number.isFinite(value) ? value : min) - min) / (max - min) * 100));
  return (
    <div className="runtime-range-field">
      <input
        className="runtime-range"
        type="range"
        aria-label={`${label}滑杆`}
        min={min}
        max={max}
        step={step}
        value={value}
        style={{ '--runtime-range-progress': `${progress}%` } as React.CSSProperties}
        onChange={(event) => onChange(Number(event.target.value))}
      />
      <div className="runtime-range-value">
        <input
          aria-label={label}
          type="number"
          min={min}
          max={max}
          step={step}
          value={value}
          onChange={(event) => onChange(Number(event.target.value))}
        />
        <span>{unit}</span>
      </div>
    </div>
  );
}

function RuntimeSettingLabel({
  title,
  variable,
  description,
}: {
  title: string;
  variable: string;
  description: string;
}) {
  return (
    <span className="runtime-setting-label">
      <strong>{title}</strong>
      <small>{variable}</small>
      <span>{description}</span>
    </span>
  );
}

function RuntimeFact({
  title,
  variable,
  value,
  detail,
  restart = false,
  restartLabel,
}: {
  title: string;
  variable: string;
  value: React.ReactNode;
  detail: string;
  restart?: boolean;
  restartLabel?: string;
}) {
  return (
    <div className="runtime-fact">
      <div className="runtime-fact-copy">
        <strong>{title}</strong>
        <small>{variable}</small>
        <span>{detail}</span>
      </div>
      <div className="runtime-fact-value">
        <span className="console-chip">{value}</span>
        <RuntimeEffectBadge restart={restart} restartLabel={restartLabel} />
      </div>
    </div>
  );
}

function callbackPurpose(callback: CallbackEndpointConfig): string {
  return callback.sourceSystem.toUpperCase() === 'EAM'
    ? '通知 EAM 文档解析完成与同步状态'
    : '通知外部系统文档同步状态变更';
}

function runtimeSettingsKey(settings: GatewayRuntimeSettings | null): string | null {
  return settings ? JSON.stringify(settings) : null;
}

export function IntegrationsPanel() {
  const [state, setState] = useState<ConsoleState<SystemIntegrations>>(initialPanelState<SystemIntegrations>);
  const [probes, setProbes] = useState<Record<string, ProbeState>>({});
  const [integrationSection, setIntegrationSection] = useState<IntegrationSection>('ragflow');
  const [runtimeDraft, setRuntimeDraft] = useState<GatewayRuntimeSettings | null>(null);
  const [runtimeBaseline, setRuntimeBaseline] = useState<string | null>(null);
  const [runtimeSaveState, setRuntimeSaveState] = useState<'idle' | 'saving' | 'saved'>('idle');
  const [runtimeSaveError, setRuntimeSaveError] = useState<DisplayError | null>(null);

  const load = useCallback(async () => {
    setState(initialPanelState<SystemIntegrations>());
    setRuntimeDraft(null);
    setRuntimeBaseline(null);
    setRuntimeSaveState('idle');
    setRuntimeSaveError(null);
    try {
      const data = await v2Api.getSystemIntegrations();
      setState({ status: 'healthy', data, error: null });
      const settings = data.runtime?.settings ?? null;
      setRuntimeDraft(settings);
      setRuntimeBaseline(runtimeSettingsKey(settings));
    } catch (error) {
      const displayError = toDisplayError(error);
      setState({ status: panelErrorStatus(displayError), data: null, error: displayError });
    }
  }, []);

  const runtimeDirty = runtimeDraft !== null && runtimeBaseline !== runtimeSettingsKey(runtimeDraft);

  const saveRuntime = useCallback(async () => {
    if (!runtimeDraft || !runtimeDirty) return;
    setRuntimeSaveState('saving');
    setRuntimeSaveError(null);
    try {
      const result = await v2Api.updateRuntimeSettings(runtimeDraft);
      setRuntimeDraft(result.settings);
      setRuntimeBaseline(runtimeSettingsKey(result.settings));
      setState((current) => current.data
        ? {
          ...current,
          data: {
            ...current.data,
            runtime: result,
            callbacksEnabled: result.settings.callbackDelivery.enabled,
            callbacks: current.data.callbacks.map((callback) => ({
              ...callback,
              enabled: result.settings.callbackDelivery.enabled,
            })),
            limits: current.data.limits
              ? {
                ...current.data.limits,
                fileShareMaxBytes: result.settings.limits.fileShareMaxMiB * 1024 * 1024,
                s3MaxBytes: result.settings.limits.s3MaxMiB * 1024 * 1024,
                transientAttachmentMaxBytes: result.settings.limits.transientAttachmentMaxMiB * 1024 * 1024,
              }
              : current.data.limits,
          },
        }
        : current);
      setRuntimeSaveState('saved');
    } catch (error) {
      setRuntimeSaveState('idle');
      setRuntimeSaveError(toDisplayError(error));
    }
  }, [runtimeDirty, runtimeDraft]);

  const patchRuntimeSection = useCallback(
    (section: keyof GatewayRuntimeSettings, patch: Record<string, number | boolean>) => {
      setRuntimeDraft((current) => {
        if (!current) return current;
        return {
          ...current,
          [section]: { ...(current[section] as object), ...patch },
        } as GatewayRuntimeSettings;
      });
      setRuntimeSaveState('idle');
    },
    [],
  );

  useEffect(() => {
    void load();
  }, [load]);

  const runProbe = useCallback(async (callback: CallbackEndpointConfig) => {
    setProbes((current) => ({ ...current, [callback.binding]: { phase: 'probing' } }));
    try {
      const result = await v2Api.probeEamCallback(callback.binding);
      setProbes((current) => ({
        ...current,
        [callback.binding]: { phase: result.status === 'connected' ? 'connected' : 'failed', result },
      }));
    } catch (error) {
      const displayError = toDisplayError(error);
      setProbes((current) => ({ ...current, [callback.binding]: { phase: 'failed', error: displayError } }));
    }
  }, []);

  const data = state.data;

  const ragflowPathLabels: Record<string, string> = {
    health: '健康检查',
    datasets: '知识库列表',
    chats: '聊天配置',
    completions: '流式问答',
    retrieval: '检索接口',
  };

  const renderRagflow = () => (
    <PanelCard
      eyebrow="RAGFlow"
      title="RAGFlow 接口配置"
      description="当前 Gateway 指向的 RAGFlow 地址与主要 API 路径，只读展示。"
      status={state.status}
      actions={(
        <button
          type="button"
          onClick={() => void load()}
          className="console-icon-button"
          aria-label="刷新接口配置"
        >
          <RefreshCw size={16} />
        </button>
      )}
      testId="console-integrations-card"
    >
      {data && (
        <div className="runtime-facts runtime-facts--api">
          <RuntimeFact
            title="RAGFlow 服务地址"
            variable="Base URL"
            value={data.ragflow.baseUrl}
            detail="Gateway 访问的 RAGFlow 服务入口"
            restart
            restartLabel="只读"
          />
          <RuntimeFact
            title="接口版本"
            variable="apiVersion"
            value={data.ragflow.apiVersion}
            detail="Gateway 使用的 RAGFlow API 版本"
            restart
            restartLabel="只读"
          />
          {Object.entries(data.ragflow.paths).map(([name, path]) => (
            <RuntimeFact
              key={name}
              title={ragflowPathLabels[name] ?? name}
              variable={name}
              value={path}
              detail="Gateway 调用的公开接口路径"
              restart
              restartLabel="只读"
            />
          ))}
        </div>
      )}
      {!data && !state.error && <p className="console-hint">RAGFlow 接口配置加载中…</p>}
      {state.error && <PanelError error={state.error} onRetry={() => void load()} />}
    </PanelCard>
  );

  const renderRuntime = () => (
    <PanelCard
      eyebrow="运行时"
      title="Gateway 运行与文件边界"
      description="中文说明当前作用；小字保留环境变量或 Worker 名称，改动后按旁边标记生效。"
      status={state.status}
      actions={(
        <button
          type="button"
          onClick={() => void load()}
          className="console-icon-button"
          aria-label="刷新运行时配置"
        >
          <RefreshCw size={16} />
        </button>
      )}
      testId="console-processing-card"
    >
      {data && (
        <>
          <div className="runtime-inline-summary" aria-label="Gateway处理口径">
            <span><strong>{formatMiB(data.limits?.fileShareMaxBytes)}</strong> · 文件共享投喂上限</span>
            <span><strong>{formatMiB(data.limits?.s3MaxBytes)}</strong> · S3 投喂上限</span>
            <span><strong>{formatMiB(data.limits?.transientAttachmentMaxBytes)} · {formatValue(data.limits?.transientAttachmentMaxFiles)} 个</strong> · 临时附件上限</span>
            <span><strong>{formatValue(data.limits?.transientAttachmentMaxFiles)} 个</strong> · 临时附件最多文件数（MAX_MESSAGE_FILES，只读）</span>
            <span><strong>{formatValue(data.gatewayProcessing?.outboxInFlight)}</strong> 个 · Outbox 单轮处理</span>
            <span><strong>{formatValue(data.gatewayProcessing?.qualityInFlight)}</strong> 个 · 质评单轮处理</span>
            <span><strong>{formatValue(data.gatewayProcessing?.callbackBatch)}</strong> 个 · 回调单轮领取，<strong>{formatValue(data.gatewayProcessing?.callbackConcurrent)}</strong> 个并行，顺序投递</span>
          </div>
          <div className="runtime-section">
            <div className="runtime-section-heading">
              <div>
                <strong>RAGFlow 解析参数</strong>
                <span>外部容器启动参数，Gateway 只读展示</span>
              </div>
              <RuntimeEffectBadge restart restartLabel="只读" />
            </div>
            <div className="runtime-facts runtime-facts--readonly">
              <RuntimeFact
                title="任务接收并发"
                variable="MAX_CONCURRENT_TASKS"
                value={`${formatValue(data.ragflow.processing?.maxConcurrentTasks)} 个`}
                detail="同时接收下载、排队等解析任务"
                restart
                restartLabel="只读"
              />
              <RuntimeFact
                title="分块与解析并发"
                variable="MAX_CONCURRENT_CHUNK_BUILDERS"
                value={`${formatValue(data.ragflow.processing?.maxConcurrentChunkBuilders)} 个`}
                detail="真正同时进行分块/解析的任务数"
                restart
                restartLabel="只读"
              />
              <RuntimeFact
                title="执行进程数"
                variable="WORKERS"
                value={`${formatValue(data.ragflow.processing?.executorWorkers)} 个`}
                detail="RAGFlow executor 启动进程数量"
                restart
                restartLabel="只读"
              />
            </div>
          </div>
          {runtimeDraft ? (
            <form
              className="runtime-settings-form"
              onSubmit={(event) => { event.preventDefault(); void saveRuntime(); }}
            >
              <div className="runtime-section-heading runtime-section-heading--editable">
                <div>
                  <strong>Gateway 可编辑参数</strong>
                  <span>开关、间隔、TTL 和文件上限都会热加载</span>
                </div>
                <RuntimeEffectBadge />
              </div>
              <div className="runtime-settings-grid">
                {([
                  ['outbox', '同步投喂任务', 'OutboxWorker', '消费文档同步 outbox，领取新的投喂任务', runtimeDraft.outbox],
                  ['statusReconciler', '文档状态回写', 'StatusReconciler', '轮询 RAGFlow，把解析状态刷回 Gateway', runtimeDraft.statusReconciler],
                  ['qualityEvaluation', '质量评估任务', 'QualityEvaluationWorker', '消费质量评估队列，逐个处理文档', runtimeDraft.qualityEvaluation],
                  ['callbackDelivery', '终态回调投递', 'CallbackDeliveryWorker', '把同步与状态变更通知给外部系统', runtimeDraft.callbackDelivery],
                ] as const).map(([section, title, variable, description, value]) => (
                  <div className="runtime-setting-row" key={section}>
                    <RuntimeSettingLabel title={title} variable={variable} description={description} />
                    <div className="runtime-setting-controls">
                      <RuntimeSwitch
                        checked={value.enabled}
                        label={`${title}启用开关`}
                        onChange={(enabled) => patchRuntimeSection(section, { enabled })}
                      />
                      <RuntimeRangeField
                        label={`${variable} 轮询间隔（秒）`}
                        value={value.pollSeconds}
                        min={0.5}
                        max={3600}
                        step={0.5}
                        unit="秒"
                        onChange={(pollSeconds) => patchRuntimeSection(section, { pollSeconds })}
                      />
                      <RuntimeEffectBadge />
                    </div>
                  </div>
                ))}
                <div className="runtime-setting-row">
                  <RuntimeSettingLabel title="临时附件清理" variable="TransientAttachmentCleanupWorker" description="清理过期临时附件，不追溯删除已有附件" />
                  <div className="runtime-setting-controls">
                    <RuntimeSwitch
                      checked={runtimeDraft.transientAttachmentCleanup.enabled}
                      label="临时附件清理启用开关"
                      onChange={(enabled) => patchRuntimeSection('transientAttachmentCleanup', { enabled })}
                    />
                    <RuntimeRangeField
                      label="附件清理轮询间隔（秒）"
                      value={runtimeDraft.transientAttachmentCleanup.pollSeconds}
                      min={0.5}
                      max={3600}
                      step={0.5}
                      unit="秒"
                      onChange={(pollSeconds) => patchRuntimeSection('transientAttachmentCleanup', { pollSeconds })}
                    />
                    <RuntimeRangeField
                      label="临时附件 TTL（秒）"
                      value={runtimeDraft.transientAttachmentCleanup.ttlSeconds}
                      min={60}
                      max={2592000}
                      unit="TTL 秒"
                      onChange={(ttlSeconds) => patchRuntimeSection('transientAttachmentCleanup', { ttlSeconds })}
                    />
                    <RuntimeEffectBadge />
                  </div>
                </div>
                <div className="runtime-setting-row">
                  <RuntimeSettingLabel title="质量状态巡检" variable="QualityReconciler" description="补发质评并把卡住的 running 任务标记失败" />
                  <div className="runtime-setting-controls">
                    <RuntimeSwitch
                      checked={runtimeDraft.qualityReconciler.enabled}
                      label="质量状态巡检启用开关"
                      onChange={(enabled) => patchRuntimeSection('qualityReconciler', { enabled })}
                    />
                    <RuntimeRangeField
                      label="质量巡检间隔（秒）"
                      value={runtimeDraft.qualityReconciler.pollSeconds}
                      min={0.5}
                      max={3600}
                      step={0.5}
                      unit="秒"
                      onChange={(pollSeconds) => patchRuntimeSection('qualityReconciler', { pollSeconds })}
                    />
                    <RuntimeRangeField
                      label="质量运行超时（秒）"
                      value={runtimeDraft.qualityReconciler.runningTimeoutSeconds}
                      min={60}
                      max={604800}
                      unit="超时秒"
                      onChange={(runningTimeoutSeconds) => patchRuntimeSection('qualityReconciler', { runningTimeoutSeconds })}
                    />
                    <RuntimeEffectBadge />
                  </div>
                </div>
                <div className="runtime-setting-row">
                  <RuntimeSettingLabel title="RAG 诊断采集" variable="ENTERPRISE_RAG_DIAGNOSTICS_ENABLED" description="记录检索、重排序和模型阶段的脱敏诊断信息" />
                  <div className="runtime-setting-controls">
                    <RuntimeSwitch
                      checked={runtimeDraft.diagnostics.enabled}
                      label="RAG 诊断采集"
                      onChange={(enabled) => patchRuntimeSection('diagnostics', { enabled })}
                    />
                    <RuntimeEffectBadge />
                  </div>
                </div>
              </div>
              <div className="runtime-section-heading runtime-section-heading--editable runtime-section-heading--limits">
                <div>
                  <strong>文件大小上限</strong>
                  <span>只能下调，不能超过 RAGFlow 解析边界</span>
                </div>
                <RuntimeEffectBadge />
              </div>
              <div className="runtime-settings-grid runtime-settings-grid--limits">
                {([
                  ['fileShareMaxMiB', '文件共享投喂', 'ENTERPRISE_FILE_SHARE_MAX_SIZE_MB', 'FILE_SHARE 单个文件可上传的最大大小', runtimeDraft.limits.fileShareMaxMiB, 128],
                  ['s3MaxMiB', 'S3 投喂', 'S3_MAX_SIZE_MB', 'S3 单个文件可上传的最大大小', runtimeDraft.limits.s3MaxMiB, 128],
                  ['transientAttachmentMaxMiB', '对话临时附件', 'ENTERPRISE_ATTACHMENT_MAX_SIZE_MB', '单个对话附件可上传的最大大小', runtimeDraft.limits.transientAttachmentMaxMiB, 10],
                ] as const).map(([field, title, variable, description, value, max]) => (
                  <div className="runtime-setting-row" key={field}>
                    <RuntimeSettingLabel title={title} variable={variable} description={description} />
                    <div className="runtime-setting-controls">
                      <RuntimeRangeField
                        label={`${field === 'fileShareMaxMiB' ? 'FILE_SHARE' : field === 's3MaxMiB' ? 'S3' : '临时附件'} 单文件上限`}
                        value={value}
                        min={1}
                        max={max}
                        unit="MiB"
                        onChange={(nextValue) => {
                          setRuntimeDraft((current) => current
                            ? { ...current, limits: { ...current.limits, [field]: nextValue } }
                            : current);
                          setRuntimeSaveState('idle');
                        }}
                      />
                      <RuntimeEffectBadge />
                    </div>
                  </div>
                ))}
              </div>
              <div className="runtime-settings-actions">
                <button
                  type="submit"
                  className="console-primary-button"
                  disabled={runtimeSaveState === 'saving' || !runtimeDirty}
                >
                  {runtimeSaveState === 'saving' ? '保存中…' : '保存并热生效'}
                </button>
                {runtimeSaveState === 'saved' && <span className="console-hint">已保存，下一轮循环生效。</span>}
                {!runtimeDirty && runtimeSaveState !== 'saved' && <span className="runtime-save-hint">当前没有待保存改动</span>}
              </div>
              {runtimeSaveError && <PanelError error={runtimeSaveError} onRetry={() => void saveRuntime()} />}
            </form>
          ) : (
            <p className="console-hint">当前 Gateway 未返回可编辑的运行时配置。</p>
          )}
        </>
      )}
      {!data && !state.error && <p className="console-hint">运行时配置加载中…</p>}
      {state.error && <PanelError error={state.error} onRetry={() => void load()} />}
    </PanelCard>
  );

  const renderCallbacks = () => (
    <PanelCard
      eyebrow="回调"
      title="回调接口配置"
      description="注册的接口用于接收文档投喂终态和状态变更通知；检测联通只发起一次探测请求。"
      status={state.status}
      testId="console-callbacks-card"
    >
      {data && !data.callbacksEnabled && <p className="console-hint">回调功能当前未启用。</p>}
      {data?.callbacks.length ? (
        <div className="console-table-wrap">
          <table className="console-table" data-testid="console-callbacks-table">
            <thead>
              <tr>
                <th>绑定</th>
                <th>来源系统</th>
                <th>租户</th>
                <th>用途</th>
                <th>方法</th>
                <th>Base URL</th>
                <th>路径</th>
                <th>启用</th>
                <th>凭据</th>
                <th className="console-col-center">联通检测</th>
              </tr>
            </thead>
            <tbody>
              {data.callbacks.map((callback) => {
                const probe: ProbeState = probes[callback.binding] ?? { phase: 'idle' };
                return (
                  <tr key={`${callback.binding}-${callback.tenantId ?? 'all'}`}>
                    <td>{callback.binding}</td>
                    <td>{callback.sourceSystem}</td>
                    <td>{callback.tenantId ?? '全部'}</td>
                    <td className="console-table-purpose">{callbackPurpose(callback)}</td>
                    <td>{callback.method}</td>
                    <td className="console-table-mono">{callback.baseUrl}</td>
                    <td className="console-table-mono">{callback.path}</td>
                    <td>{callback.enabled ? '启用' : '停用'}</td>
                    <td>{callback.credentialConfigured ? '已配置' : '未配置'}</td>
                    <td>
                      <div className="console-probe-cell">
                        <button
                          type="button"
                          className="console-secondary-button"
                          disabled={probe.phase === 'probing'}
                          onClick={() => void runProbe(callback)}
                        >
                          检测联通
                        </button>
                        <ProbeBadge state={probe} />
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        data && <p className="console-empty">暂无回调配置。</p>
      )}
      {!data && !state.error && <p className="console-hint">接口配置加载中…</p>}
    </PanelCard>
  );

  return (
    <div className="console-integration-shell">
      <nav className="console-subnav" aria-label="接口配置子菜单" role="tablist">
        {([
          ['ragflow', 'RAGFlow 接口', '地址与 API 路径'],
          ['runtime', 'Gateway 运行', '文件边界与任务参数'],
          ['callbacks', '回调接口', '通知外部系统'],
        ] as const).map(([id, label, detail]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={integrationSection === id}
            className={`console-subnav-item ${integrationSection === id ? 'is-active' : ''}`}
            onClick={() => setIntegrationSection(id)}
          >
            <strong>{label}</strong>
            <span>{detail}</span>
          </button>
        ))}
      </nav>
      {integrationSection === 'ragflow' && renderRagflow()}
      {integrationSection === 'runtime' && renderRuntime()}
      {integrationSection === 'callbacks' && renderCallbacks()}
    </div>
  );
}

// ---- Shared metadata building blocks (toolbars, summary, sort, pills) ------

export interface MetadataSortState {
  orderBy: string | null;
  order: MetadataSortOrder;
}

export const DEFAULT_SORT_STATE: MetadataSortState = { orderBy: null, order: 'desc' };

/** 未排序 → desc → asc → 清除（回到服务端默认排序）。 */
export function nextSortState(current: MetadataSortState, field: string): MetadataSortState {
  if (current.orderBy !== field) return { orderBy: field, order: 'desc' };
  if (current.order === 'desc') return { orderBy: field, order: 'asc' };
  return DEFAULT_SORT_STATE;
}

/** 业务状态色板：绿=正常、红=失败、橙=需关注、蓝=处理中、灰=其他。 */
const STATUS_PILL_TONES: Record<string, string> = {
  ready: 'ok',
  active: 'ok',
  completed: 'ok',
  failed: 'failed',
  review_required: 'warn',
  no_reliable_evidence: 'warn',
  parsing: 'processing',
  processing: 'processing',
  running: 'processing',
  registered: 'muted',
  cancelled: 'muted',
  archived: 'muted',
  superseded: 'muted',
  disabled: 'muted',
};

export function StatusPill({ code, label }: { code: string | null | undefined; label?: string }) {
  const value = code ?? '';
  const tone = STATUS_PILL_TONES[value] ?? 'muted';
  return (
    <span className={`console-status console-status--${tone}`}>
      <span className="console-status-dot" aria-hidden="true" />
      {label ?? (value || NOT_PROVIDED)}
    </span>
  );
}

export function SortableTh({
  label,
  field,
  sort,
  onSort,
}: {
  label: string;
  field: string;
  sort: MetadataSortState;
  onSort: (field: string) => void;
}) {
  const active = sort.orderBy === field;
  const Icon = !active ? ArrowUpDown : sort.order === 'asc' ? ArrowUp : ArrowDown;
  return (
    <th aria-sort={active ? (sort.order === 'asc' ? 'ascending' : 'descending') : 'none'}>
      <button
        type="button"
        className={`console-th-btn${active ? ' is-active' : ''}`}
        onClick={() => onSort(field)}
      >
        {label}
        <Icon size={12} aria-hidden="true" />
      </button>
    </th>
  );
}

export interface MetadataActiveFilter {
  key: string;
  label: string;
  value: string;
  onClear: () => void;
}

export function MetadataToolbar({
  onApply,
  onReset,
  activeFilters,
  totalCount,
  totalLabel,
  extra,
  children,
}: {
  onApply?: () => void;
  onReset: () => void;
  activeFilters: MetadataActiveFilter[];
  totalCount: number | null;
  totalLabel: string;
  extra?: React.ReactNode;
  children: React.ReactNode;
}) {
  const controls = (
    <>
      {children}
      {onApply && <button type="submit" className="console-secondary-button">筛选</button>}
      <button type="button" className="console-secondary-button" onClick={onReset}>重置</button>
    </>
  );
  return (
    <div className="console-toolbar">
      {onApply ? (
        <form onSubmit={(event) => { event.preventDefault(); onApply(); }}>{controls}</form>
      ) : (
        <div className="console-toolbar-controls">{controls}</div>
      )}
      <span className="console-toolbar-spacer" aria-hidden="true" />
      {extra}
      <div className="console-toolbar-status">
        {activeFilters.map((filter) => (
          <span key={filter.key} className="console-filter-chip">
            {filter.label} {filter.value}
            <button type="button" aria-label={`清除${filter.label}筛选`} onClick={filter.onClear}>×</button>
          </span>
        ))}
        <span className="console-chip">
          {totalCount != null ? `${totalLabel} ${totalCount}` : '数据来源 · Gateway 元数据'}
        </span>
      </div>
    </div>
  );
}

function ToolbarSelect({
  id,
  label,
  value,
  options,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  options: readonly string[];
  onChange: (value: string) => void;
}) {
  return (
    <>
      <label htmlFor={id}>{label}</label>
      <select id={id} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">全部</option>
        {options.map((option) => (
          <option key={option} value={option}>{option}</option>
        ))}
      </select>
    </>
  );
}

export interface MetadataSummaryChip {
  key: string;
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}

export function MetadataSummaryStrip({
  chips,
  testId,
}: {
  chips: MetadataSummaryChip[];
  testId: string;
}) {
  return (
    <div className="console-summary" data-testid={testId}>
      {chips.map((chip) => (
        <button
          key={chip.key}
          type="button"
          className={`console-chip console-summary-chip${chip.active ? ' is-active' : ''}`}
          onClick={chip.onClick}
        >
          {chip.label} {chip.count}
        </button>
      ))}
    </div>
  );
}

export function MetadataPagination({
  offset,
  itemCount,
  hasMore,
  onPrev,
  onNext,
  pageSize = PAGE_LIMIT,
  onPageSizeChange,
}: {
  offset: number;
  itemCount: number;
  hasMore: boolean;
  onPrev: () => void;
  onNext: () => void;
  pageSize?: number;
  onPageSizeChange?: (pageSize: number) => void;
}) {
  const pageNumber = Math.floor(Math.max(0, offset) / Math.max(1, pageSize)) + 1;
  return (
    <PaginationBar
      page={pageNumber}
      itemCount={itemCount}
      hasMore={hasMore}
      pageSize={pageSize}
      onPageSizeChange={onPageSizeChange}
      onPrevious={onPrev}
      onNext={onNext}
    />
  );
}

// ---- 会话元数据 ------------------------------------------------------------

type ConversationColumnKey =
  | 'conversationId'
  | 'businessUserId'
  | 'equipmentId'
  | 'fixedAssetNo'
  | 'status'
  | 'contextVersion'
  | 'ragflow'
  | 'createdAt'
  | 'lastMessageAt';

const CONVERSATION_COLUMNS: Array<{ key: ConversationColumnKey; label: string; sortField: ConversationMetadataOrderBy | null; fixed?: boolean }> = [
  { key: 'conversationId', label: '会话', sortField: 'conversationId', fixed: true },
  { key: 'businessUserId', label: '业务用户', sortField: 'businessUserId' },
  { key: 'equipmentId', label: '设备', sortField: 'equipmentId' },
  { key: 'fixedAssetNo', label: '固定资产', sortField: 'fixedAssetNo' },
  { key: 'status', label: '状态', sortField: 'status' },
  { key: 'contextVersion', label: 'Context', sortField: 'contextVersion' },
  { key: 'ragflow', label: 'RAGFlow', sortField: null },
  { key: 'createdAt', label: '创建时间', sortField: 'createdAt' },
  { key: 'lastMessageAt', label: '最近消息', sortField: 'lastMessageAt' },
];

const CONVERSATION_HIDDEN_COLUMNS_KEY = 'console.convMeta.hiddenColumns';
const CONVERSATION_DEFAULT_HIDDEN_COLUMNS: string[] = [];
type ConversationFilterKey = keyof Required<ConversationMetadataFilters>;
type ConversationFilterDraft = Record<ConversationFilterKey, string>;

const EMPTY_CONVERSATION_FILTERS: ConversationFilterDraft = {
  conversationId: '',
  businessUserId: '',
  equipmentId: '',
  fixedAssetNo: '',
  ragflowId: '',
  contextVersion: '',
};

const CONVERSATION_ADVANCED_FIELDS: Array<{
  key: ConversationFilterKey;
  label: string;
  placeholder: string;
  type?: 'search' | 'number';
}> = [
  { key: 'conversationId', label: '会话 ID', placeholder: '输入完整会话 ID' },
  { key: 'businessUserId', label: '业务用户', placeholder: '输入业务用户标识' },
  { key: 'equipmentId', label: '设备编号', placeholder: '输入设备编号' },
  { key: 'fixedAssetNo', label: '固定资产编号', placeholder: '输入固定资产编号' },
  { key: 'ragflowId', label: 'RAGFlow 会话 ID', placeholder: '匹配 Chat 或 Session ID' },
  { key: 'contextVersion', label: 'Context 版本', placeholder: '例如 1', type: 'number' },
];

function hasConversationFilters(filters: ConversationFilterDraft): boolean {
  return Object.values(filters).some((value) => value.trim() !== '');
}

function normaliseConversationFilters(filters: ConversationFilterDraft): ConversationFilterDraft {
  return Object.fromEntries(
    Object.entries(filters).map(([key, value]) => [key, value.trim()]),
  ) as ConversationFilterDraft;
}

function renderConversationCell(item: ConversationMetadataItem, key: ConversationColumnKey): React.ReactNode {
  switch (key) {
    case 'conversationId':
      return <td key={key} className="console-table-mono">{item.conversationId}</td>;
    case 'businessUserId':
      return <td key={key}>{item.businessUserId}</td>;
    case 'equipmentId':
      return <td key={key}>{formatValue(item.equipmentId)}</td>;
    case 'fixedAssetNo':
      return <td key={key}>{formatValue(item.fixedAssetNo)}</td>;
    case 'status':
      return <td key={key}><StatusPill code={item.status} /></td>;
    case 'contextVersion':
      return <td key={key}>v{item.contextVersion}</td>;
    case 'ragflow':
      return <td key={key} className="console-table-mono">{item.ragflowChatId ?? item.ragflowSessionId ?? NOT_PROVIDED}</td>;
    case 'createdAt':
      return <td key={key}>{formatTime(item.createdAt)}</td>;
    case 'lastMessageAt':
      return <td key={key}>{formatTime(item.lastMessageAt)}</td>;
    default:
      return null;
  }
}

export function ConversationMetadataPanel() {
  const [state, setState] = useState<ConsoleState<ConversationMetadataPage>>(initialPanelState<ConversationMetadataPage>);
  const [statusFilter, setStatusFilter] = useState('');
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [advancedDraft, setAdvancedDraft] = useState<ConversationFilterDraft>(EMPTY_CONVERSATION_FILTERS);
  const [advancedFilters, setAdvancedFilters] = useState<ConversationFilterDraft>(EMPTY_CONVERSATION_FILTERS);
  const advancedTriggerRef = useRef<HTMLButtonElement | null>(null);
  const [sort, setSort] = useState<MetadataSortState>(DEFAULT_SORT_STATE);
  const [offset, setOffset] = useState(0);
  const [pageSize, setPageSize] = useState(PAGE_LIMIT);
  const [requestToken, setRequestToken] = useState(0);
  const [summary, setSummary] = useState<MetadataSummary | null>(null);
  const [selectedConversation, setSelectedConversation] = useState<ConversationMetadataItem | null>(null);
  const { hiddenColumns: hiddenConversationColumns, visibleColumns: visibleConversationColumns, toggleColumn: toggleConversationColumn, resetColumns: resetConversationColumns } = useHiddenTableColumns(
    CONVERSATION_HIDDEN_COLUMNS_KEY,
    CONVERSATION_DEFAULT_HIDDEN_COLUMNS,
    CONVERSATION_COLUMNS,
  );

  const reload = useCallback(() => setRequestToken((token) => token + 1), []);

  const load = useCallback(async () => {
    setState(initialPanelState<ConversationMetadataPage>());
    const contextVersion = advancedFilters.contextVersion.trim();
    try {
      const data = await v2Api.listAdminConversationMetadata({
        limit: pageSize,
        offset,
        status: statusFilter || null,
        filters: {
          conversationId: advancedFilters.conversationId || null,
          businessUserId: advancedFilters.businessUserId || null,
          equipmentId: advancedFilters.equipmentId || null,
          fixedAssetNo: advancedFilters.fixedAssetNo || null,
          ragflowId: advancedFilters.ragflowId || null,
          contextVersion: /^\d+$/.test(contextVersion) ? Number(contextVersion) : null,
        },
        orderBy: sort.orderBy as ConversationMetadataOrderBy | null,
        order: sort.orderBy ? sort.order : null,
      });
      setState({ status: 'healthy', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setState({ status: panelErrorStatus(displayError), data: null, error: displayError });
    }
  }, [advancedFilters, offset, pageSize, sort, statusFilter]);

  const loadSummary = useCallback(async () => {
    try {
      setSummary(await v2Api.getMetadataSummary());
    } catch {
      // 汇总失败静默降级，不阻断主表。
      setSummary(null);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, requestToken]);

  useEffect(() => {
    void loadSummary();
  }, [loadSummary]);

  const applyFilters = useCallback(() => {
    setAdvancedFilters(normaliseConversationFilters(advancedDraft));
    setOffset(0);
    setAdvancedOpen(false);
  }, [advancedDraft]);

  const resetFilters = useCallback(() => {
    setStatusFilter('');
    setAdvancedDraft(EMPTY_CONVERSATION_FILTERS);
    setAdvancedFilters(EMPTY_CONVERSATION_FILTERS);
    setSort(DEFAULT_SORT_STATE);
    setOffset(0);
  }, []);

  const clearAdvancedFilter = useCallback((key: ConversationFilterKey) => {
    setAdvancedDraft((current) => ({ ...current, [key]: '' }));
    setAdvancedFilters((current) => ({ ...current, [key]: '' }));
    setOffset(0);
  }, []);

  const clearConversationFilters = useCallback(() => {
    setStatusFilter('');
    setAdvancedDraft(EMPTY_CONVERSATION_FILTERS);
    setAdvancedFilters(EMPTY_CONVERSATION_FILTERS);
    setOffset(0);
  }, []);

  const handleSort = useCallback((field: string) => {
    setSort((current) => nextSortState(current, field));
    setOffset(0);
  }, []);

  const activeFilters = useMemo<MetadataActiveFilter[]>(() => {
    const filters: MetadataActiveFilter[] = [];
    if (statusFilter) {
      filters.push({ key: 'status', label: '状态', value: statusFilter, onClear: () => { setStatusFilter(''); setOffset(0); } });
    }
    for (const field of CONVERSATION_ADVANCED_FIELDS) {
      const value = advancedFilters[field.key];
      if (value) {
        filters.push({
          key: field.key,
          label: field.label,
          value,
          onClear: () => clearAdvancedFilter(field.key),
        });
      }
    }
    return filters;
  }, [advancedFilters, clearAdvancedFilter, statusFilter]);

  const summaryChips = useMemo<MetadataSummaryChip[]>(() => {
    const byStatus = summary?.conversations.byStatus ?? {};
    const chips: MetadataSummaryChip[] = [
      {
        key: 'total',
        label: '会话',
        count: summary?.conversations.total ?? 0,
        active: !statusFilter && !hasConversationFilters(advancedFilters),
        onClick: clearConversationFilters,
      },
    ];
    for (const [status, count] of Object.entries(byStatus)) {
      chips.push({
        key: `status-${status}`,
        label: status,
        count,
        active: statusFilter === status,
        onClick: () => { setStatusFilter(statusFilter === status ? '' : status); setOffset(0); },
      });
    }
    return chips;
  }, [advancedFilters, clearConversationFilters, statusFilter, summary]);

  const page = state.data;

  return (
    <>
    <PanelCard
      eyebrow="Metadata"
      title="会话元数据"
      description="每行代表一个 Gateway v2 会话；只读索引，不回显消息正文。需要查看问答内容请进入“会话管理”。"
      status={state.status}
      actions={(
        <button
          type="button"
          onClick={() => { reload(); void loadSummary(); }}
          className="console-icon-button"
          aria-label="刷新会话元数据"
        >
          <RefreshCw size={16} />
        </button>
      )}
      testId="console-meta-conversations-card"
      className="console-table-card"
    >
      <div className="console-info-banner" role="note">
        <div>
          <strong>这里显示什么？</strong>
          <p>这是会话索引，不是消息正文；一行对应一个 Gateway v2 会话。</p>
        </div>
        <p>会话 ID 用于定位，业务用户 / 设备 / 固定资产用于归属，状态、Context 版本、RAGFlow ID 和时间用于运行排查。</p>
        <span>可用上方筛选或“高级检索”组合条件；需要查看完整问答、思考中状态和引用，请进入“会话管理”。</span>
      </div>
      <MetadataToolbar
        onApply={applyFilters}
        onReset={resetFilters}
        activeFilters={activeFilters}
        totalCount={summary?.conversations.total ?? null}
        totalLabel="会话"
        extra={(
          <ColumnMenu
            columns={CONVERSATION_COLUMNS}
            hiddenColumns={hiddenConversationColumns}
            onToggle={toggleConversationColumn}
            onReset={resetConversationColumns}
          />
        )}
      >
        <ToolbarSelect
          id="conversation-status-filter"
          label="状态"
          value={statusFilter}
          options={CONVERSATION_STATUS_OPTIONS}
          onChange={(value) => { setStatusFilter(value); setOffset(0); }}
        />
        <button
          type="button"
          className={`console-secondary-button console-advanced-toggle${advancedOpen ? ' is-active' : ''}`}
          ref={advancedTriggerRef}
          aria-expanded={advancedOpen}
          aria-controls="conversation-advanced-search"
          onClick={() => setAdvancedOpen((open) => !open)}
        >
          <SlidersHorizontal size={14} aria-hidden="true" />
          高级检索
        </button>
      </MetadataToolbar>
      <ConsoleOverlay
        open={advancedOpen}
        mode="dialog"
        onClose={() => setAdvancedOpen(false)}
        ariaLabel="会话高级检索"
        className="console-advanced-search-overlay"
      >
        <section
          id="conversation-advanced-search"
          className="console-advanced-search"
          data-testid="console-conversation-advanced-search"
          aria-label="会话高级检索条件"
        >
          <div className="console-advanced-search-head">
            <div>
              <strong>组合条件</strong>
              <span>多个条件同时满足；未填写的条件不会参与筛选</span>
            </div>
            <button type="button" className="console-text-button" onClick={() => setAdvancedDraft(EMPTY_CONVERSATION_FILTERS)}>
              清空条件
            </button>
          </div>
          <div className="console-advanced-search-grid">
            {CONVERSATION_ADVANCED_FIELDS.map((field) => (
              <label key={field.key} htmlFor={`conversation-filter-${field.key}`}>
                <span>{field.label}</span>
                <input
                  id={`conversation-filter-${field.key}`}
                  type={field.type ?? 'search'}
                  min={field.type === 'number' ? 0 : undefined}
                  step={field.type === 'number' ? 1 : undefined}
                  value={advancedDraft[field.key]}
                  placeholder={field.placeholder}
                  onChange={(event) => setAdvancedDraft((current) => ({ ...current, [field.key]: event.target.value }))}
                />
              </label>
            ))}
          </div>
          <div className="console-advanced-search-actions">
            <button type="button" className="console-secondary-button" onClick={() => setAdvancedOpen(false)}>取消</button>
            <button type="button" className="console-primary-button" onClick={applyFilters}>应用条件</button>
          </div>
        </section>
      </ConsoleOverlay>
      {summary && (
        <MetadataSummaryStrip chips={summaryChips} testId="console-conversations-summary" />
      )}
      {page?.items.length ? (
        <div className="console-table-wrap">
          <table className="console-table" data-testid="console-meta-conversations-table">
            <thead>
              <tr>
                {visibleConversationColumns.map((column) => (
                  column.sortField
                    ? <SortableTh key={column.key} label={column.label} field={column.sortField} sort={sort} onSort={handleSort} />
                    : <th key={column.key}>{column.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {page.items.map((item) => (
                <tr
                  key={item.conversationId}
                  data-row-action="true"
                  tabIndex={0}
                  onClick={() => setSelectedConversation(item)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      setSelectedConversation(item);
                    }
                  }}
                >
                  {visibleConversationColumns.map((column) => renderConversationCell(item, column.key))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="console-empty">
          {state.status === 'processing' ? '会话元数据加载中…' : '暂无会话元数据。'}
        </p>
      )}
      <MetadataPagination
        offset={offset}
        itemCount={page?.items.length ?? 0}
        hasMore={Boolean(page?.hasMore)}
        pageSize={pageSize}
        onPageSizeChange={(value) => { setPageSize(value); setOffset(0); }}
        onPrev={() => setOffset(Math.max(0, offset - pageSize))}
        onNext={() => setOffset(offset + pageSize)}
      />
      {state.error && <PanelError error={state.error} onRetry={reload} />}
    </PanelCard>
    {selectedConversation && <ConversationInspector conversation={selectedConversation} onClose={() => setSelectedConversation(null)} />}
    </>
  );
}

// ---- 文件元数据 ------------------------------------------------------------

type DocColumnKey =
  | 'externalDocumentId'
  | 'fileName'
  | 'sourceSystem'
  | 'documentType'
  | 'equipmentId'
  | 'fixedAssetNo'
  | 'assetId'
  | 'syncStatus'
  | 'businessStatus'
  | 'ragflow'
  | 'sourceSize'
  | 'createdAt'
  | 'updatedAt'
  | 'parsedAt'
  | 'eamNotifiedAt';

const DOC_COLUMNS: Array<{ key: DocColumnKey; label: string; sortField: DocumentMetadataOrderBy | null; fixed?: boolean }> = [
  { key: 'externalDocumentId', label: '文档', sortField: 'externalDocumentId', fixed: true },
  { key: 'fileName', label: '文件名', sortField: 'fileName' },
  { key: 'sourceSystem', label: '来源系统', sortField: 'sourceSystem' },
  { key: 'documentType', label: '类型', sortField: 'documentType' },
  { key: 'equipmentId', label: '设备', sortField: 'equipmentId' },
  { key: 'fixedAssetNo', label: '固定资产', sortField: 'fixedAssetNo' },
  { key: 'assetId', label: '资产', sortField: 'assetId' },
  { key: 'syncStatus', label: '同步状态', sortField: 'syncStatus' },
  { key: 'businessStatus', label: '业务状态', sortField: 'businessStatus' },
  { key: 'ragflow', label: 'RAGFlow', sortField: null },
  { key: 'sourceSize', label: '大小', sortField: 'sourceSize' },
  { key: 'createdAt', label: '创建时间', sortField: 'createdAt' },
  { key: 'updatedAt', label: '更新时间', sortField: 'updatedAt' },
  { key: 'parsedAt', label: 'RAGFlow解析完成', sortField: 'parsedAt' },
  { key: 'eamNotifiedAt', label: 'EAM通知时间', sortField: 'eamNotifiedAt' },
];

function renderDocumentCell(item: DocumentMetadataItem, key: DocColumnKey): React.ReactNode {
  switch (key) {
    case 'externalDocumentId':
      return <td key={key} className="console-table-mono">{item.externalDocumentId}</td>;
    case 'fileName':
      return <td key={key}>{item.fileName}</td>;
    case 'sourceSystem':
      return <td key={key}>{item.sourceSystem}</td>;
    case 'documentType':
      return <td key={key}>{formatValue(item.documentType)}</td>;
    case 'equipmentId':
      return <td key={key}>{formatValue(item.equipmentId)}</td>;
    case 'fixedAssetNo':
      return <td key={key}>{formatValue(item.fixedAssetNo)}</td>;
    case 'assetId':
      return <td key={key}>{formatValue(item.assetId)}</td>;
    case 'syncStatus':
      return <td key={key}><StatusPill code={item.syncStatus} /></td>;
    case 'businessStatus':
      return <td key={key}><StatusPill code={item.businessStatus} /></td>;
    case 'ragflow':
      return (
        <td key={key} className="console-table-mono">
          {item.ragflowDatasetId || item.ragflowDocumentId
            ? `dataset ${item.ragflowDatasetId ?? '-'} / doc ${item.ragflowDocumentId ?? '-'}`
            : NOT_PROVIDED}
        </td>
      );
    case 'sourceSize':
      return <td key={key}>{formatValue(item.sourceSize)}</td>;
    case 'createdAt':
      return <td key={key}>{formatTime(item.createdAt)}</td>;
    case 'updatedAt':
      return <td key={key}>{formatTime(item.updatedAt)}</td>;
    case 'parsedAt':
      return <td key={key}>{formatTime(item.parsedAt)}</td>;
    case 'eamNotifiedAt':
      return <td key={key}>{formatTime(item.eamNotifiedAt)}</td>;
    default:
      return null;
  }
}

export interface ConsoleColumnDefinition {
  key: string;
  label: string;
  /** 固定列在菜单里禁用勾选，且即使用户曾隐藏过也始终显示（如主键列、操作列）。 */
  fixed?: boolean;
}

function readHiddenColumns(storageKey: string, defaults: string[]): string[] {
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return defaults;
    const parsed: unknown = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return parsed.filter((value): value is string => typeof value === 'string');
    }
  } catch {
    // localStorage 不可用（隐私模式等）时退回默认预设。
  }
  return defaults;
}

export function useHiddenTableColumns<T extends ConsoleColumnDefinition>(
  storageKey: string,
  defaultHidden: string[],
  columns: ReadonlyArray<T>,
) {
  const [hiddenColumns, setHiddenColumns] = useState<string[]>(
    () => readHiddenColumns(storageKey, defaultHidden),
  );

  const toggleColumn = useCallback((key: string) => {
    setHiddenColumns((current) => {
      const next = current.includes(key)
        ? current.filter((value) => value !== key)
        : [...current, key];
      try {
        localStorage.setItem(storageKey, JSON.stringify(next));
      } catch {
        // 隐私模式下持久化失败可忽略，仅影响本次会话。
      }
      return next;
    });
  }, [storageKey]);

  const resetColumns = useCallback(() => {
    setHiddenColumns(defaultHidden);
    try {
      localStorage.setItem(storageKey, JSON.stringify(defaultHidden));
    } catch {
      // 忽略持久化失败。
    }
  }, [defaultHidden, storageKey]);

  const visibleColumns = useMemo<ReadonlyArray<T>>(
    () => columns.filter((column) => column.fixed || !hiddenColumns.includes(column.key)),
    [columns, hiddenColumns],
  );

  return { hiddenColumns, visibleColumns, toggleColumn, resetColumns };
}

export function ColumnMenu({
  columns,
  hiddenColumns,
  onToggle,
  onReset,
}: {
  columns: ReadonlyArray<ConsoleColumnDefinition>;
  hiddenColumns: string[];
  onToggle: (key: string) => void;
  onReset: () => void;
}) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return undefined;
    const handlePointerDown = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handlePointerDown);
    return () => document.removeEventListener('mousedown', handlePointerDown);
  }, [open]);

  return (
    <div className="console-col-wrap" ref={menuRef}>
      <button
        type="button"
        className="console-icon-button"
        aria-label="列显示设置"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <Columns3 size={16} />
      </button>
      {open && (
        <div className="console-col-menu" role="group" aria-label="表格列显示">
          <p className="console-col-menu-title">显示列</p>
          {columns.map((column) => (
            <label key={column.key} className="console-col-menu-row">
              <input
                type="checkbox"
                checked={column.fixed || !hiddenColumns.includes(column.key)}
                disabled={column.fixed}
                onChange={() => onToggle(column.key)}
              />
              {column.label}
            </label>
          ))}
          <div className="console-col-menu-actions">
            <button type="button" className="console-secondary-button" onClick={onReset}>恢复默认</button>
          </div>
        </div>
      )}
    </div>
  );
}

export function DocumentMetadataPanel() {
  const [state, setState] = useState<ConsoleState<DocumentMetadataPage>>(initialPanelState<DocumentMetadataPage>);
  const [sourceDraft, setSourceDraft] = useState('');
  const [sourceFilter, setSourceFilter] = useState('');
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [advancedDraft, setAdvancedDraft] = useState<DocumentFilterDraft>(EMPTY_DOCUMENT_FILTERS);
  const [advancedFilters, setAdvancedFilters] = useState<DocumentFilterDraft>(EMPTY_DOCUMENT_FILTERS);
  const advancedTriggerRef = useRef<HTMLButtonElement | null>(null);
  const [syncStatusFilter, setSyncStatusFilter] = useState('');
  const [businessStatusFilter, setBusinessStatusFilter] = useState('');
  const [sort, setSort] = useState<MetadataSortState>(DEFAULT_SORT_STATE);
  const { hiddenColumns, visibleColumns: visibleDocColumns, toggleColumn: toggleDocColumn, resetColumns: resetDocColumns } = useHiddenTableColumns(
    DOC_HIDDEN_COLUMNS_KEY,
    DOC_DEFAULT_HIDDEN_COLUMNS,
    DOC_COLUMNS,
  );
  const [offset, setOffset] = useState(0);
  const [pageSize, setPageSize] = useState(PAGE_LIMIT);
  const [requestToken, setRequestToken] = useState(0);
  const [summary, setSummary] = useState<MetadataSummary | null>(null);
  const [selectedDocument, setSelectedDocument] = useState<DocumentMetadataItem | null>(null);

  const reload = useCallback(() => setRequestToken((token) => token + 1), []);

  const load = useCallback(async () => {
    setState(initialPanelState<DocumentMetadataPage>());
    try {
      const data = await v2Api.listAdminDocumentMetadata({
        limit: pageSize,
        offset,
        sourceSystem: sourceFilter || null,
        status: syncStatusFilter || null,
        businessStatus: businessStatusFilter || null,
        filters: {
          externalDocumentId: advancedFilters.externalDocumentId || null,
          sourceVersionId: advancedFilters.sourceVersionId || null,
          fileName: advancedFilters.fileName || null,
          equipmentId: advancedFilters.equipmentId || null,
          fixedAssetNo: advancedFilters.fixedAssetNo || null,
          assetId: advancedFilters.assetId || null,
          ragflowDocumentId: advancedFilters.ragflowDocumentId || null,
        },
        orderBy: sort.orderBy as DocumentMetadataOrderBy | null,
        order: sort.orderBy ? sort.order : null,
      });
      setState({ status: 'healthy', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setState({ status: panelErrorStatus(displayError), data: null, error: displayError });
    }
  }, [advancedFilters, businessStatusFilter, offset, pageSize, sort, sourceFilter, syncStatusFilter]);

  const loadSummary = useCallback(async () => {
    try {
      setSummary(await v2Api.getMetadataSummary());
    } catch {
      // 汇总失败静默降级，不阻断主表。
      setSummary(null);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, requestToken]);

  useEffect(() => {
    void loadSummary();
  }, [loadSummary]);

  const applyFilters = useCallback(() => {
    setSourceFilter(sourceDraft.trim());
    setAdvancedFilters(normaliseDocumentFilters(advancedDraft));
    setOffset(0);
    setAdvancedOpen(false);
  }, [advancedDraft, sourceDraft]);

  const clearAllFilters = useCallback(() => {
    setSourceDraft('');
    setSourceFilter('');
    setAdvancedDraft(EMPTY_DOCUMENT_FILTERS);
    setAdvancedFilters(EMPTY_DOCUMENT_FILTERS);
    setSyncStatusFilter('');
    setBusinessStatusFilter('');
    setOffset(0);
  }, []);

  const resetFilters = useCallback(() => {
    clearAllFilters();
    setSort(DEFAULT_SORT_STATE);
  }, [clearAllFilters]);

  const handleSort = useCallback((field: string) => {
    setSort((current) => nextSortState(current, field));
    setOffset(0);
  }, []);

  const activeFilters = useMemo<MetadataActiveFilter[]>(() => {
    const filters: MetadataActiveFilter[] = [];
    if (sourceFilter) {
      filters.push({ key: 'sourceSystem', label: '来源系统', value: sourceFilter, onClear: () => { setSourceFilter(''); setOffset(0); } });
    }
    if (syncStatusFilter) {
      filters.push({ key: 'syncStatus', label: '同步状态', value: syncStatusFilter, onClear: () => { setSyncStatusFilter(''); setOffset(0); } });
    }
    if (businessStatusFilter) {
      filters.push({ key: 'businessStatus', label: '业务状态', value: businessStatusFilter, onClear: () => { setBusinessStatusFilter(''); setOffset(0); } });
    }
    for (const field of DOCUMENT_ADVANCED_FIELDS) {
      const value = advancedFilters[field.key];
      if (value) {
        filters.push({
          key: field.key,
          label: field.label,
          value,
          onClear: () => {
            setAdvancedDraft((current) => ({ ...current, [field.key]: '' }));
            setAdvancedFilters((current) => ({ ...current, [field.key]: '' }));
            setOffset(0);
          },
        });
      }
    }
    return filters;
  }, [advancedFilters, businessStatusFilter, sourceFilter, syncStatusFilter]);

  const summaryChips = useMemo<MetadataSummaryChip[]>(() => {
    const bySync = summary?.documents.bySyncStatus ?? {};
    const byBusiness = summary?.documents.byBusinessStatus ?? {};
    const chips: MetadataSummaryChip[] = [
      {
        key: 'total',
        label: '文档',
        count: summary?.documents.total ?? 0,
        active: !sourceFilter && !syncStatusFilter && !businessStatusFilter && !hasDocumentFilters(advancedFilters),
        onClick: clearAllFilters,
      },
    ];
    for (const [status, count] of Object.entries(bySync)) {
      chips.push({
        key: `sync-${status}`,
        label: status,
        count,
        active: syncStatusFilter === status,
        onClick: () => { setSyncStatusFilter(syncStatusFilter === status ? '' : status); setOffset(0); },
      });
    }
    const reviewRequired = byBusiness.review_required;
    if (reviewRequired && reviewRequired > 0) {
      chips.push({
        key: 'business-review_required',
        label: 'review_required',
        count: reviewRequired,
        active: businessStatusFilter === 'review_required',
        onClick: () => {
          setBusinessStatusFilter(businessStatusFilter === 'review_required' ? '' : 'review_required');
          setOffset(0);
        },
      });
    }
    return chips;
  }, [advancedFilters, businessStatusFilter, clearAllFilters, sourceFilter, summary, syncStatusFilter]);

  const page = state.data;

  return (
    <>
    <PanelCard
      eyebrow="Metadata"
      title="文件元数据"
      description="管理员视角的文件元数据；展示来源系统、同步状态与 RAGFlow 映射。"
      status={state.status}
      actions={(
        <button
          type="button"
          onClick={() => { reload(); void loadSummary(); }}
          className="console-icon-button"
          aria-label="刷新文件元数据"
        >
          <RefreshCw size={16} />
        </button>
      )}
      testId="console-meta-documents-card"
      className="console-table-card"
    >
      <MetadataToolbar
        onApply={applyFilters}
        onReset={resetFilters}
        activeFilters={activeFilters}
        totalCount={summary?.documents.total ?? null}
        totalLabel="文档"
        extra={(
          <ColumnMenu
            columns={DOC_COLUMNS}
            hiddenColumns={hiddenColumns}
            onToggle={toggleDocColumn}
            onReset={resetDocColumns}
          />
        )}
      >
        <label htmlFor="document-source-filter">来源系统</label>
        <input
          id="document-source-filter"
          value={sourceDraft}
          placeholder="如 EAM"
          onChange={(event) => setSourceDraft(event.target.value)}
        />
        <ToolbarSelect
          id="document-sync-filter"
          label="同步状态"
          value={syncStatusFilter}
          options={SYNC_STATUS_OPTIONS}
          onChange={(value) => { setSyncStatusFilter(value); setOffset(0); }}
        />
        <ToolbarSelect
          id="document-business-filter"
          label="业务状态"
          value={businessStatusFilter}
          options={BUSINESS_STATUS_OPTIONS}
          onChange={(value) => { setBusinessStatusFilter(value); setOffset(0); }}
        />
        <button
          type="button"
          ref={advancedTriggerRef}
          className={`console-secondary-button console-advanced-toggle${advancedOpen ? ' is-active' : ''}`}
          aria-expanded={advancedOpen}
          aria-controls="document-advanced-search"
          onClick={() => setAdvancedOpen((open) => !open)}
        >
          <SlidersHorizontal size={14} aria-hidden="true" />
          高级检索
        </button>
      </MetadataToolbar>
      <ConsoleOverlay
        open={advancedOpen}
        mode="dialog"
        onClose={() => setAdvancedOpen(false)}
        ariaLabel="文件高级检索"
        className="console-advanced-search-overlay"
      >
        <section id="document-advanced-search" className="console-advanced-search" data-testid="console-document-advanced-search" aria-label="文件高级检索条件">
          <div className="console-advanced-search-head">
            <div>
              <strong>组合条件</strong>
              <span>多个条件同时满足；未填写的条件不会参与筛选</span>
            </div>
            <button type="button" className="console-text-button" onClick={() => setAdvancedDraft(EMPTY_DOCUMENT_FILTERS)}>清空条件</button>
          </div>
          <div className="console-advanced-search-grid">
            {DOCUMENT_ADVANCED_FIELDS.map((field) => (
              <label key={field.key} htmlFor={`document-filter-${field.key}`}>
                <span>{field.label}</span>
                <input
                  id={`document-filter-${field.key}`}
                  value={advancedDraft[field.key]}
                  placeholder={field.placeholder}
                  onChange={(event) => setAdvancedDraft((current) => ({ ...current, [field.key]: event.target.value }))}
                />
              </label>
            ))}
          </div>
          <div className="console-advanced-search-actions">
            <button type="button" className="console-secondary-button" onClick={() => setAdvancedOpen(false)}>取消</button>
            <button type="button" className="console-primary-button" onClick={applyFilters}>应用条件</button>
          </div>
        </section>
      </ConsoleOverlay>
      {summary && (
        <MetadataSummaryStrip chips={summaryChips} testId="console-documents-summary" />
      )}
      {page?.items.length ? (
        <div className="console-table-wrap">
          <table className="console-table" data-testid="console-meta-documents-table">
            <thead>
              <tr>
                {visibleDocColumns.map((column) => (
                  column.sortField
                    ? <SortableTh key={column.key} label={column.label} field={column.sortField} sort={sort} onSort={handleSort} />
                    : <th key={column.key}>{column.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {page.items.map((item) => (
                <tr
                  key={`${item.externalDocumentId}-${item.sourceVersionId}`}
                  data-row-action="true"
                  tabIndex={0}
                  onClick={() => setSelectedDocument(item)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      setSelectedDocument(item);
                    }
                  }}
                >
                  {visibleDocColumns.map((column) => renderDocumentCell(item, column.key))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="console-empty">
          {state.status === 'processing' ? '文件元数据加载中…' : '暂无文件元数据。'}
        </p>
      )}
      <MetadataPagination
        offset={offset}
        itemCount={page?.items.length ?? 0}
        hasMore={Boolean(page?.hasMore)}
        pageSize={pageSize}
        onPageSizeChange={(value) => { setPageSize(value); setOffset(0); }}
        onPrev={() => setOffset(Math.max(0, offset - pageSize))}
        onNext={() => setOffset(offset + pageSize)}
      />
      {state.error && <PanelError error={state.error} onRetry={reload} />}
    </PanelCard>
    {selectedDocument && <DocumentInspector document={selectedDocument} onClose={() => setSelectedDocument(null)} />}
    </>
  );
}
