import { useCallback, useEffect, useMemo, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { API_MODE } from '../api/mode';
import { toDisplayError, v2Api } from '../api/v2Client';
import type {
  ConsoleState,
  ConsoleUserPrincipal,
  GatewayHealth,
} from '../api/consoleTypes';
import type {
  ConversationAttachmentResponse,
} from '../api/v2Types';
import { TransientAttachmentPanel } from '../components/harness/TransientAttachmentPanel';
import { ConversationAdminPanel } from '../components/console/ConversationAdminPanel';
import { RagDiagnosticsPanel } from '../components/console/RagDiagnosticsPanel';
import { ChunkManagementPanel } from '../components/console/ChunkManagementPanel';
import { ConversationMetadataPanel, DocumentMetadataPanel, IntegrationsPanel } from '../components/console/SystemSettingsPanels';
import { EquipmentIdentityPanel } from '../components/console/EquipmentIdentityPanel';
import { PanelBadge, PanelCard, PanelError, initialPanelState, panelErrorStatus } from '../components/common/Panel';
import { WorkbenchShell, useWorkbenchTab } from '../components/layout/WorkbenchShell';

const CONSOLE_TABS = [
  'service',
  'attachment',
  'integrations',
  'meta-conversations',
  'conversation-admin',
  'meta-documents',
  'equipment-identities',
  'meta-chunks',
  'rag-diagnostics',
] as const;
type ConsoleTab = (typeof CONSOLE_TABS)[number];

const CONSOLE_NAV = [
  {
    id: 'diagnostics',
    label: '诊断',
    items: [
      { id: 'service', label: '服务状态' },
      { id: 'attachment', label: '临时附件' },
    ],
  },
];

const SYSTEM_NAV_GROUP = {
  id: 'system',
  label: '系统设置',
  items: [
    { id: 'integrations', label: '接口配置' },
    { id: 'meta-conversations', label: '会话元数据' },
    { id: 'conversation-admin', label: '会话管理' },
    { id: 'meta-documents', label: '文件元数据' },
    { id: 'equipment-identities', label: '设备标识' },
    { id: 'meta-chunks', label: '解析 Chunk' },
    { id: 'rag-diagnostics', label: 'RAG 诊断' },
  ],
};

const SYSTEM_TAB_IDS: readonly string[] = SYSTEM_NAV_GROUP.items.map((item) => item.id);

async function encodeAttachment(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result !== 'string') {
        reject(new Error('Attachment content could not be read'));
        return;
      }
      const separator = reader.result.indexOf(',');
      resolve(separator >= 0 ? reader.result.slice(separator + 1) : reader.result);
    };
    reader.onerror = () => reject(reader.error ?? new Error('Attachment content could not be read'));
    reader.readAsDataURL(file);
  });
}

function ProbeRow({
  label,
  route,
  state,
}: {
  label: string;
  route: string;
  state: ConsoleState<unknown>;
}) {
  return (
    <div className="console-row">
      <div>
        <p>{label}</p>
        <p className="console-route">{route}</p>
      </div>
      <PanelBadge status={state.status} />
    </div>
  );
}

function ServicePanel({
  health,
  identity,
  onRefresh,
}: {
  health: ConsoleState<GatewayHealth>;
  identity: ConsoleState<ConsoleUserPrincipal>;
  onRefresh: () => void;
}) {
  const status = health.status === 'healthy' && identity.status === 'healthy'
    ? 'healthy'
    : health.status === 'unavailable' || identity.status === 'unavailable'
      ? 'unavailable'
      : health.status === 'unauthorized' || identity.status === 'unauthorized'
        ? 'unauthorized'
        : 'processing';
  const modeLabel = API_MODE === 'mock' ? 'mock / MSW' : `${API_MODE} / public Gateway routes`;
  return (
    <PanelCard
      eyebrow="Gateway"
      title="服务与用户边界"
      description="只探测公开 Gateway；健康探针与认证会话分开显示，任何一项失败都不会阻断其他卡片。"
      status={status}
      actions={<button type="button" onClick={onRefresh} className="console-icon-button" aria-label="刷新服务状态"><RefreshCw size={16} /></button>}
      testId="console-service-card"
    >
      <ProbeRow label="Gateway liveness" route="GET /enterprise/api/v1/health" state={health} />
      <ProbeRow label="User scope" route="GET /enterprise/api/v1/auth/me" state={identity} />
      <div className="console-note">
        <p><strong>运行边界</strong> · {modeLabel}</p>
        <p>{API_MODE === 'gateway'
          ? '生产 WebUI 使用同源 HttpOnly 本地运维会话；HMAC secret 和密码不会进入浏览器。'
          : '浏览器只携带用户 Bearer。HMAC secret 不进入浏览器；User JWT 沿用现有 Harness 的 sessionStorage 生命周期，Console 不展示 JWT。'}</p>
        {health.data && <p className="console-route">gateway version · {health.data.version}</p>}
        {identity.data && <p>用户映射：{identity.data.mappingStatus} · capabilities {identity.data.capabilities.length}</p>}
      </div>
      {health.error && <PanelError error={health.error} onRetry={onRefresh} />}
      {identity.error && <PanelError error={identity.error} onRetry={onRefresh} />}
    </PanelCard>
  );
}

function AttachmentPanel({
  activeId,
  state,
  attachment,
  notice,
  onUpload,
  onIssueTicket,
  onDownload,
}: {
  activeId: string | null;
  state: ConsoleState<ConversationAttachmentResponse>;
  attachment: ConversationAttachmentResponse | null;
  notice: string | null;
  onUpload: (file: File) => void;
  onIssueTicket: () => void;
  onDownload: () => void;
}) {
  return (
    <div data-testid="console-attachment-card" className="console-card console-embedded">
      <div className="console-card-head">
        <div>
          <p className="console-eyebrow">Attachment</p>
          <h2>Transient attachment</h2>
          <p>正式 create → ticket → download 诊断入口。失败只影响本卡片，不改变 FILE_SHARE 或会话状态。</p>
        </div>
        <PanelBadge status={state.status === 'processing' ? 'processing' : state.status} />
      </div>
      <div className="console-card-body">
        {!activeId && <p className="console-hint">请先在 Harness 问答会话中创建会话。</p>}
        <TransientAttachmentPanel
          conversationId={activeId}
          loading={state.status === 'processing'}
          error={state.error}
          notice={notice}
          attachment={attachment}
          status={state.status}
          issueLoading={state.status === 'processing'}
          downloadLoading={state.status === 'processing'}
          onUpload={onUpload}
          onIssueTicket={onIssueTicket}
          onDownload={onDownload}
        />
      </div>
    </div>
  );
}

export function EnterpriseConsolePage() {
  const [tab, setTab] = useWorkbenchTab<ConsoleTab>('service', CONSOLE_TABS);
  const [health, setHealth] = useState<ConsoleState<GatewayHealth>>(initialPanelState<GatewayHealth>);
  const [identity, setIdentity] = useState<ConsoleState<ConsoleUserPrincipal>>(initialPanelState<ConsoleUserPrincipal>);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [attachmentState, setAttachmentState] = useState<ConsoleState<ConversationAttachmentResponse>>({ status: 'configured', data: null, error: null });
  const [attachment, setAttachment] = useState<ConversationAttachmentResponse | null>(null);
  const [attachmentNotice, setAttachmentNotice] = useState<string | null>(null);
  const isAdmin = identity.data?.capabilities.includes('admin') ?? false;
  const navGroups = useMemo(
    () => (isAdmin ? [...CONSOLE_NAV, SYSTEM_NAV_GROUP] : CONSOLE_NAV),
    [isAdmin],
  );

  const loadHealth = useCallback(async () => {
    setHealth({ status: 'processing', data: null, error: null });
    try {
      const data = await v2Api.getHealth();
      setHealth({ status: data.status === 'healthy' ? 'healthy' : 'failed', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setHealth({ status: panelErrorStatus(displayError), data: null, error: displayError });
    }
  }, []);

  const loadIdentity = useCallback(async () => {
    setIdentity({ status: 'processing', data: null, error: null });
    try {
      const data = await v2Api.getAuthMe();
      setIdentity({ status: 'healthy', data, error: null });
    } catch (error) {
      const displayError = toDisplayError(error);
      setIdentity({ status: panelErrorStatus(displayError), data: null, error: displayError });
    }
  }, []);

  // The attachment diagnostics card still needs an owned conversation id, but
  // the duplicate Console session-history viewer has been removed. Harness is
  // the interactive conversation surface; System Settings owns administration.
  const loadActiveConversation = useCallback(async () => {
    try {
      const page = await v2Api.listConversations();
      setActiveId((current) => current ?? page.items[0]?.conversationId ?? null);
    } catch {
      // Service and identity cards report authentication/availability errors;
      // without a session the attachment card remains safely unavailable.
    }
  }, []);

  useEffect(() => {
    void loadHealth();
    void loadIdentity();
    void loadActiveConversation();
  }, [loadActiveConversation, loadHealth, loadIdentity]);

  useEffect(() => {
    setAttachment(null);
    setAttachmentState({ status: 'configured', data: null, error: null });
    setAttachmentNotice(null);
  }, [activeId]);

  const refreshAll = useCallback(() => {
    void loadHealth();
    void loadIdentity();
    void loadActiveConversation();
  }, [loadActiveConversation, loadHealth, loadIdentity]);

  const uploadAttachment = useCallback(async (file: File) => {
    if (!activeId) return;
    setAttachmentState({ status: 'processing', data: null, error: null });
    setAttachmentNotice(null);
    try {
      const created = await v2Api.createConversationAttachment(activeId, {
        fileName: file.name,
        mediaType: file.type || 'application/octet-stream',
        content: await encodeAttachment(file),
      });
      const ticketed = await v2Api.issueConversationAttachmentTicket(created.attachmentId);
      setAttachment(ticketed);
      setAttachmentState({ status: 'retrievable', data: ticketed, error: null });
      setAttachmentNotice('create 与 ticket 已完成；下载响应体不会在 Console 展示。');
    } catch (error) {
      const displayError = toDisplayError(error);
      setAttachmentState({ status: panelErrorStatus(displayError), data: null, error: displayError });
    }
  }, [activeId]);

  const issueTicket = useCallback(async () => {
    if (!attachment) return;
    setAttachmentState({ status: 'processing', data: attachment, error: null });
    setAttachmentNotice(null);
    try {
      const ticketed = await v2Api.issueConversationAttachmentTicket(attachment.attachmentId);
      setAttachment(ticketed);
      setAttachmentState({ status: 'retrievable', data: ticketed, error: null });
      setAttachmentNotice('新下载票据已签发；票据本身不会显示。');
    } catch (error) {
      const displayError = toDisplayError(error);
      setAttachmentState({ status: panelErrorStatus(displayError), data: attachment, error: displayError });
    }
  }, [attachment]);

  const verifyDownload = useCallback(async () => {
    if (!attachment) return;
    setAttachmentState({ status: 'processing', data: attachment, error: null });
    setAttachmentNotice(null);
    try {
      const result = await v2Api.verifyConversationAttachmentDownload(attachment);
      setAttachmentState({ status: 'retrievable', data: attachment, error: null });
      setAttachmentNotice(`download route verified · ${result.sizeBytes} bytes · ${result.contentType}`);
    } catch (error) {
      const displayError = toDisplayError(error);
      setAttachmentState({ status: panelErrorStatus(displayError), data: attachment, error: displayError });
    }
  }, [attachment]);

  return (
    <WorkbenchShell
      testId="console-page"
      shellClass="console-shell"
      brand="Console"
      subtitle="Gateway diagnostics"
      groups={navGroups}
      activeId={tab}
      onSelect={(id) => setTab(id as ConsoleTab)}
      actions={null}
      tokenRow={null}
      footer={(
        <footer className="console-footer">
          <span>Active trace: {activeId ? 'conversation selected' : 'no conversation selected'}</span>
          <span className="console-route">route /console · Attachment contract v2.1.0</span>
        </footer>
      )}
    >
      {tab === 'service' && (
        <ServicePanel health={health} identity={identity} onRefresh={refreshAll} />
      )}
      {tab === 'attachment' && (
        <AttachmentPanel
          activeId={activeId}
          state={attachmentState}
          attachment={attachment}
          notice={attachmentNotice}
          onUpload={(file) => void uploadAttachment(file)}
          onIssueTicket={() => void issueTicket()}
          onDownload={() => void verifyDownload()}
        />
      )}
      {isAdmin && tab === 'integrations' && <IntegrationsPanel />}
      {isAdmin && tab === 'meta-conversations' && <ConversationMetadataPanel />}
      {isAdmin && tab === 'conversation-admin' && <ConversationAdminPanel />}
      {isAdmin && tab === 'meta-documents' && <DocumentMetadataPanel />}
      {isAdmin && tab === 'equipment-identities' && <EquipmentIdentityPanel />}
      {isAdmin && tab === 'meta-chunks' && <ChunkManagementPanel />}
      {isAdmin && tab === 'rag-diagnostics' && <RagDiagnosticsPanel />}
      {!isAdmin && SYSTEM_TAB_IDS.includes(tab) && (
        <p className="console-empty">需要 admin capability 才能查看系统设置。</p>
      )}
    </WorkbenchShell>
  );
}
