import { useCallback, useEffect, useState } from 'react';
import { RefreshCw, Search } from 'lucide-react';
import { toDisplayError, v2Api } from '../../api/v2Client';
import type {
  EquipmentIdentityPage,
  EquipmentRecognitionPreview,
  EquipmentRecognitionSettings,
} from '../../api/consoleTypes';
import { PanelCard, PanelError, initialPanelState, panelErrorStatus } from '../common/Panel';
import { MetadataPagination } from '../common/MetadataControls';

function syncLabel(status: string): string {
  switch (status) {
    case 'done':
      return '已同步';
    case 'processing':
      return '同步中';
    case 'failed':
    case 'dead':
      return '同步失败';
    case 'not_queued':
      return '未排队';
    default:
      return status || '待同步';
  }
}

function value(value: string | null): string {
  return value || '—';
}

function openDocumentMetadata(equipmentId: string): void {
  const query = new URLSearchParams(window.location.search);
  query.set('equipmentId', equipmentId);
  const search = query.toString();
  window.history.replaceState(
    null,
    '',
    `${window.location.pathname}${search ? `?${search}` : ''}#/meta-documents`,
  );
  window.dispatchEvent(new Event('hashchange'));
}

export function EquipmentIdentityPanel() {
  const [state, setState] = useState(initialPanelState<EquipmentIdentityPage>);
  const [searchDraft, setSearchDraft] = useState('');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [recognition, setRecognition] = useState<EquipmentRecognitionSettings | null>(null);
  const [patternDraft, setPatternDraft] = useState('');
  const [enabledDraft, setEnabledDraft] = useState(true);
  const [ruleNotice, setRuleNotice] = useState<string | null>(null);
  const [previewText, setPreviewText] = useState('');
  const [preview, setPreview] = useState<EquipmentRecognitionPreview | null>(null);

  const load = useCallback(async () => {
    setState({ status: 'processing', data: null, error: null });
    try {
      const data = await v2Api.listEquipmentIdentities({
        limit: pageSize,
        offset,
        identifier: search || null,
      });
      setState({ status: 'healthy', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setState({ status: panelErrorStatus(displayError), data: null, error: displayError });
    }
  }, [offset, pageSize, search]);

  const loadRecognition = useCallback(async () => {
    try {
      const data = await v2Api.getEquipmentRecognition();
      setRecognition(data);
      setPatternDraft(data.pattern);
      setEnabledDraft(data.enabled);
    } catch (error) {
      setRuleNotice(toDisplayError(error).message);
    }
  }, []);

  useEffect(() => {
    void load();
    void loadRecognition();
  }, [load, loadRecognition]);

  const applySearch = () => {
    setOffset(0);
    setSearch(searchDraft.trim());
  };

  const saveRecognition = async () => {
    if (!recognition) return;
    setRuleNotice(null);
    try {
      const data = await v2Api.updateEquipmentRecognition({
        ...recognition,
        pattern: patternDraft,
        enabled: enabledDraft,
      });
      setRecognition(data);
      setPatternDraft(data.pattern);
      setEnabledDraft(data.enabled);
      setRuleNotice('识别规则已保存，新对话生效。');
    } catch (error) {
      setRuleNotice(toDisplayError(error).message);
    }
  };

  const runPreview = async () => {
    setRuleNotice(null);
    try {
      setPreview(await v2Api.previewEquipmentRecognition(previewText, patternDraft || undefined));
    } catch (error) {
      setRuleNotice(toDisplayError(error).message);
    }
  };

  const retrySync = async (equipmentId: string) => {
    setRuleNotice(null);
    try {
      await v2Api.retryEquipmentIdentitySync(equipmentId);
      await load();
    } catch (error) {
      setRuleNotice(toDisplayError(error).message);
    }
  };

  const page = state.data;

  return (
    <>
      <PanelCard
        eyebrow="Identity"
        title="设备标识"
        description="查看 EAM 当前生效的设备标识，并按稳定设备号打开文件元数据。"
        status={state.status}
        actions={(
          <button type="button" onClick={() => void load()} className="console-icon-button" aria-label="刷新设备标识">
            <RefreshCw size={16} />
          </button>
        )}
        testId="console-equipment-identities-card"
        className="console-table-card"
      >
        <div className="console-toolbar">
          <label htmlFor="equipment-identity-search">设备标识</label>
          <input
            id="equipment-identity-search"
            value={searchDraft}
            placeholder="设备号、固定资产号或资产 ID"
            onChange={(event) => setSearchDraft(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') applySearch(); }}
          />
          <button type="button" className="console-primary-button" onClick={applySearch}>
            <Search size={14} aria-hidden="true" /> 查询
          </button>
        </div>
        <div className="console-table-wrap" data-testid="console-equipment-identities-table">
          <table className="console-table">
            <thead>
              <tr>
                <th>设备号</th>
                <th>固定资产号</th>
                <th>资产 ID</th>
                <th>版本</th>
                <th>文件</th>
                <th>RAGFlow 元数据</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {(page?.items ?? []).map((item) => (
                <tr key={item.equipmentId}>
                  <td className="console-table-mono">{item.equipmentId}</td>
                  <td>{value(item.fixedAssetNo)}</td>
                  <td>{value(item.assetId)}</td>
                  <td>v{item.identityVersion}</td>
                  <td>{item.documentCount}</td>
                  <td>{syncLabel(item.sync.status)}</td>
                  <td>
                    <button
                      type="button"
                      className="console-secondary-button"
                      onClick={() => openDocumentMetadata(item.equipmentId)}
                    >
                      查看文件元数据
                    </button>
                    {(item.sync.status === 'failed' || item.sync.status === 'dead') && (
                      <button
                        type="button"
                        className="console-secondary-button"
                        onClick={() => void retrySync(item.equipmentId)}
                      >
                        重试同步
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {state.status === 'processing' && (
                <tr><td colSpan={7}>设备标识加载中…</td></tr>
              )}
              {state.status !== 'processing' && page && page.items.length === 0 && (
                <tr><td colSpan={7}>暂无设备标识。</td></tr>
              )}
            </tbody>
          </table>
        </div>
        <MetadataPagination
          offset={offset}
          itemCount={page?.items.length ?? 0}
          hasMore={Boolean(page?.hasMore)}
          pageSize={pageSize}
          onPageSizeChange={(value) => { setPageSize(value); setOffset(0); }}
          onPrev={() => setOffset(Math.max(0, offset - pageSize))}
          onNext={() => setOffset(offset + pageSize)}
        />
        {state.error && <PanelError error={state.error} onRetry={() => void load()} />}
      </PanelCard>

      <PanelCard
        eyebrow="Recognition"
        title="设备号识别规则"
        description="只配置候选编号提取；设备映射、型号排除和权限边界仍由 Gateway 固定处理。"
        status={recognition ? 'healthy' : 'processing'}
        testId="console-equipment-recognition-card"
      >
        <label htmlFor="equipment-recognition-pattern">候选编号正则</label>
        <input
          id="equipment-recognition-pattern"
          value={patternDraft}
          onChange={(event) => setPatternDraft(event.target.value)}
          spellCheck={false}
        />
        <label className="console-checkbox">
          <input
            type="checkbox"
            checked={enabledDraft}
            onChange={(event) => setEnabledDraft(event.target.checked)}
          />
          启用设备号识别
        </label>
        <button type="button" className="console-primary-button" onClick={() => void saveRecognition()}>
          保存规则
        </button>
        <label htmlFor="equipment-recognition-preview">预览文本</label>
        <textarea
          id="equipment-recognition-preview"
          value={previewText}
          onChange={(event) => setPreviewText(event.target.value)}
          placeholder="输入一段包含设备号的提问"
          rows={3}
        />
        <button type="button" className="console-secondary-button" onClick={() => void runPreview()}>
          预览候选
        </button>
        {preview && (
          <div className="console-note" data-testid="equipment-recognition-preview-result">
            {preview.items.length === 0
              ? '未提取到候选编号。'
              : preview.items.map((item) => (
                <p key={item.candidate}>
                  <span className="console-table-mono">{item.candidate}</span>
                  {' · '}
                  {item.matched ? `匹配设备 ${item.equipmentId}` : '未匹配当前设备'}
                </p>
              ))}
          </div>
        )}
        {ruleNotice && <p className="console-note">{ruleNotice}</p>}
      </PanelCard>
    </>
  );
}
