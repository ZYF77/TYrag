import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { delay, http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { EnterpriseConsolePage } from '../pages/EnterpriseConsolePage';
import { server } from '../test-setup';

const V1 = '/enterprise/api/v1';
const DOC_HIDDEN_COLUMNS_KEY = 'console.docMeta.hiddenColumns';
const CONVERSATION_HIDDEN_COLUMNS_KEY = 'console.convMeta.hiddenColumns';

const adminPrincipal = {
  businessUserId: 'admin-user',
  displayName: 'admin-user',
  tenantId: 'wp04e2e2',
  departmentIds: ['d10'],
  roles: ['admin'],
  capabilities: ['read', 'ask', 'list_sessions', 'view_citations', 'admin'],
  securityLevel: 9,
  mappingStatus: 'active',
};

const integrationsBody = {
  ragflow: {
    baseUrl: 'http://ragflow.internal:9380',
    apiVersion: 'v1',
    paths: {
      health: '/api/v1/system/ping',
      datasets: '/api/v1/datasets',
      chats: '/api/v1/chats',
      completions: '/api/v1/chat/completions',
      retrieval: '/api/v1/retrieval',
    },
  },
  callbacksEnabled: true,
  callbacks: [
    {
      binding: 'EAM',
      tenantId: null,
      sourceSystem: 'EAM',
      baseUrl: 'https://eam.example',
      path: '/callback',
      method: 'POST',
      enabled: true,
      credentialConfigured: true,
    },
    {
      binding: 'CMMS',
      tenantId: 'tenant-b',
      sourceSystem: 'CMMS',
      baseUrl: 'https://cmms.example',
      path: '/hooks/tyrag',
      method: 'POST',
      enabled: false,
      credentialConfigured: false,
    },
    {
      binding: 'LEGACY',
      tenantId: null,
      sourceSystem: 'LEGACY',
      baseUrl: 'https://legacy.example',
      path: '/notify',
      method: 'GET',
      enabled: true,
      credentialConfigured: true,
    },
  ],
  // 契约之外的敏感字段：UI 只渲染契约字段，该值不得出现在 DOM。
  credentialFingerprint: 'SUPERSECRET-FINGERPRINT',
};

const conversationItems = [
  {
    conversationId: 'conv-meta-1',
    businessUserId: 'user-a',
    equipmentId: 'EQ-1001',
    fixedAssetNo: 'FA-2001',
    status: 'active',
    ragflowChatId: 'chat-1',
    ragflowSessionId: 'sess-1',
    contextVersion: 3,
    createdAt: '2026-08-29T10:00:00.000Z',
    lastMessageAt: '2026-08-29T11:00:00.000Z',
  },
  {
    conversationId: 'conv-meta-2',
    businessUserId: 'user-b',
    equipmentId: null,
    fixedAssetNo: null,
    status: 'closed',
    ragflowChatId: null,
    ragflowSessionId: 'sess-2',
    contextVersion: 1,
    createdAt: '2026-08-28T10:00:00.000Z',
    lastMessageAt: null,
  },
];

const documentItems = [
  {
    externalDocumentId: 'ext-doc-meta-1',
    sourceVersionId: 'v3',
    fileName: 'AX-200维修手册.pdf',
    sourceSystem: 'EAM',
    documentType: 'manual',
    equipmentId: 'EQ-1001',
    fixedAssetNo: 'FA-2001',
    assetId: 'ASSET-1',
    syncStatus: 'ready',
    businessStatus: 'active',
    ragflowDatasetId: 'ds-1',
    ragflowDocumentId: 'rag-doc-1',
    sourceSize: 12345,
    createdAt: '2026-08-29T09:00:00.000Z',
    updatedAt: '2026-08-29T09:30:00.000Z',
    parsedAt: '2026-08-29T09:20:00.000Z',
    eamNotifiedAt: '2026-08-29T09:40:00.000Z',
  },
  {
    externalDocumentId: 'ext-doc-meta-2',
    sourceVersionId: 'v1',
    fileName: '保养清单.xlsx',
    sourceSystem: 'CMMS',
    documentType: null,
    equipmentId: null,
    fixedAssetNo: null,
    assetId: null,
    syncStatus: null,
    businessStatus: 'active',
    ragflowDatasetId: null,
    ragflowDocumentId: null,
    sourceSize: null,
    createdAt: '2026-08-27T09:00:00.000Z',
    updatedAt: null,
    parsedAt: null,
    eamNotifiedAt: null,
  },
];

const summaryBody = {
  conversations: { total: 3, byStatus: { active: 2, archived: 1 } },
  documents: {
    total: 13,
    bySyncStatus: { ready: 9, failed: 4 },
    byBusinessStatus: { active: 10, review_required: 3 },
  },
};

const adminConversationMessages = {
  conversationId: 'conv-meta-1',
  items: [
    {
      messageId: 'msg-u1',
      role: 'user',
      content: 'AX-200 报警 E-104 应该怎么处理？\n请给出步骤。',
      status: 'completed',
      createdAt: '2026-08-29T10:00:00.000Z',
    },
    {
      messageId: 'msg-a1',
      role: 'assistant',
      content: '请先检查**液压油位**，再复位告警。',
      status: 'completed',
      createdAt: '2026-08-29T10:00:05.000Z',
    },
    {
      messageId: 'msg-a2',
      role: 'assistant',
      content: '没有找到可靠依据，无法回答该问题。',
      status: 'no_reliable_evidence',
      createdAt: '2026-08-29T10:01:00.000Z',
    },
  ],
};

function adminScenario() {
  return [
    http.get(`${V1}/auth/me`, () => HttpResponse.json(adminPrincipal)),
    http.post(`${V1}/admin/system/eam-probe`, async ({ request }) => {
      const body = (await request.json()) as { binding?: string };
      if (body.binding === 'EAM') {
        await delay(100);
        return HttpResponse.json({
          binding: 'EAM',
          probeUrl: 'https://eam.example/.well-known/jwks.json',
          status: 'connected',
          httpStatus: 200,
          latencyMs: 123,
          checkedAt: '2026-08-30T08:00:00.000Z',
          errorCode: null,
        });
      }
      if (body.binding === 'CMMS') {
        return HttpResponse.json({
          binding: 'CMMS',
          probeUrl: 'https://cmms.example/.well-known/jwks.json',
          status: 'failed',
          httpStatus: null,
          latencyMs: 42,
          checkedAt: '2026-08-30T08:00:00.000Z',
          errorCode: 'PROBE_CONNECT_FAILED',
        });
      }
      return HttpResponse.json(
        {
          code: 'PROBE_TARGET_NOT_FOUND',
          message: 'Unknown callback binding',
          requestId: 'probe-unknown',
          retryable: false,
        },
        { status: 404 },
      );
    }),
  ];
}

function integrationsHandler() {
  return http.get(`${V1}/admin/system/integrations`, () => HttpResponse.json(integrationsBody));
}

function conversationsMetadataHandler(
  urls?: string[],
  page: { items: typeof conversationItems; hasMore: boolean } = { items: conversationItems, hasMore: true },
) {
  return http.get(`${V1}/admin/system/metadata/conversations`, ({ request }) => {
    urls?.push(new URL(request.url).search);
    return HttpResponse.json(page);
  });
}

function documentsMetadataHandler(
  urls?: string[],
  page: { items: typeof documentItems; hasMore: boolean } = { items: documentItems, hasMore: false },
) {
  return http.get(`${V1}/admin/system/metadata/documents`, ({ request }) => {
    urls?.push(new URL(request.url).search);
    return HttpResponse.json(page);
  });
}

function metadataSummaryHandler(urls?: string[], body = summaryBody) {
  return http.get(`${V1}/admin/system/metadata/summary`, ({ request }) => {
    urls?.push(new URL(request.url).search);
    return HttpResponse.json(body);
  });
}

function conversationMessagesHandler(
  options: { body?: typeof adminConversationMessages; status?: number; urls?: string[] } = {},
) {
  return http.get(
    `${V1}/admin/system/metadata/conversations/:conversationId/messages`,
    ({ request }) => {
      options.urls?.push(new URL(request.url).pathname);
      if (options.status && options.status !== 200) {
        return HttpResponse.json(
          {
            code: 'CONVERSATION_NOT_FOUND',
            message: '会话不存在或不属于当前租户',
            requestId: 'req-messages-404',
            retryable: false,
          },
          { status: options.status },
        );
      }
      return HttpResponse.json(options.body ?? adminConversationMessages);
    },
  );
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.removeItem(DOC_HIDDEN_COLUMNS_KEY);
  localStorage.removeItem(CONVERSATION_HIDDEN_COLUMNS_KEY);
  localStorage.removeItem('console.convAdmin.hiddenColumns');
});

describe('SystemSettingsPanels (admin system settings)', () => {
  it('shows the system settings group only for admin capability', async () => {
    server.use(...adminScenario());
    const adminView = render(<EnterpriseConsolePage />);
    expect(await adminView.findByRole('button', { name: '接口配置' })).toBeTruthy();
    expect(adminView.getByRole('button', { name: '会话元数据' })).toBeTruthy();
    expect(adminView.getByRole('button', { name: '会话管理' })).toBeTruthy();
    expect(adminView.getByRole('button', { name: '文件元数据' })).toBeTruthy();
    adminView.unmount();

    server.use(
      http.get(
        `${V1}/auth/me`,
        () =>
          HttpResponse.json({
            ...adminPrincipal,
            capabilities: ['read', 'ask', 'list_sessions', 'view_citations'],
          }),
      ),
    );
    window.location.hash = '#/integrations';
    const userView = render(<EnterpriseConsolePage />);
    expect(await userView.findByText('需要 admin capability 才能查看系统设置。')).toBeTruthy();
    expect(userView.queryByRole('button', { name: '接口配置' })).toBeNull();
    expect(userView.queryByRole('button', { name: '会话管理' })).toBeNull();
    expect(userView.queryByTestId('console-integrations-card')).toBeNull();
    userView.unmount();
    window.location.hash = '';
  });

  it('renders integration config fields and probes rows independently', async () => {
    server.use(...adminScenario(), integrationsHandler(), conversationsMetadataHandler(), metadataSummaryHandler());
    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    await user.click(await screen.findByRole('button', { name: '接口配置' }));

    const ragflowCard = await screen.findByTestId('console-integrations-card');
    expect(ragflowCard.textContent).toContain('http://ragflow.internal:9380');
    expect(ragflowCard.textContent).toContain('/api/v1/system/ping');
    expect(ragflowCard.textContent).toContain('/api/v1/chat/completions');

    const table = await screen.findByTestId('console-callbacks-table');
    const rows = within(table).getAllByRole('row');
    expect(rows.length).toBe(4); // header + 3 callbacks

    const eamRow = rows[1];
    expect(eamRow.textContent).toContain('EAM');
    expect(eamRow.textContent).toContain('https://eam.example');
    expect(eamRow.textContent).toContain('/callback');
    expect(eamRow.textContent).toContain('POST');
    expect(eamRow.textContent).toContain('启用');
    expect(eamRow.textContent).toContain('已配置');
    const cmmsRow = rows[2];
    expect(cmmsRow.textContent).toContain('https://cmms.example');
    expect(cmmsRow.textContent).toContain('/hooks/tyrag');
    expect(cmmsRow.textContent).toContain('tenant-b');
    expect(cmmsRow.textContent).toContain('停用');
    expect(cmmsRow.textContent).toContain('未配置');

    // EAM probe: 检测中 -> connected（含 latencyMs）
    const eamProbe = within(eamRow).getByRole('button', { name: '检测联通' });
    await user.click(eamProbe);
    expect((eamProbe as HTMLButtonElement).disabled).toBe(true);
    await screen.findByText('检测中');
    await screen.findByText(/connected · HTTP 200 · 123ms/);
    expect((eamProbe as HTMLButtonElement).disabled).toBe(false);

    // CMMS probe 失败：只影响本行，EAM 行仍显示 connected
    const cmmsProbe = within(cmmsRow).getByRole('button', { name: '检测联通' });
    await user.click(cmmsProbe);
    await screen.findByText(/failed · PROBE_CONNECT_FAILED/);
    expect(screen.getByText(/connected · HTTP 200 · 123ms/)).toBeTruthy();

    // 其他面板不受 probe 失败影响
    await user.click(screen.getByRole('button', { name: '会话元数据' }));
    await screen.findByTestId('console-meta-conversations-table');
  });

  it('surfaces probe HTTP errors like unknown binding per row', async () => {
    server.use(...adminScenario(), integrationsHandler());
    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    await user.click(await screen.findByRole('button', { name: '接口配置' }));
    const table = await screen.findByTestId('console-callbacks-table');
    const rows = within(table).getAllByRole('row');

    await user.click(within(rows[3]).getByRole('button', { name: '检测联通' }));
    await screen.findByText(/failed · PROBE_TARGET_NOT_FOUND/);

    // 未探测的行保持空闲，无探针结果
    expect(rows[1].textContent).not.toContain('connected');
    expect(rows[2].textContent).not.toContain('failed');
  });

  it('renders conversation metadata with filters, sorting and pagination', async () => {
    const urls: string[] = [];
    server.use(...adminScenario(), metadataSummaryHandler(), conversationsMetadataHandler(urls));
    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    await user.click(await screen.findByRole('button', { name: '会话元数据' }));
    const table = await screen.findByTestId('console-meta-conversations-table');
    expect(within(table).getAllByRole('row').length).toBe(3); // header + 2 items
    expect(table.textContent).toContain('conv-meta-1');
    expect(table.textContent).toContain('user-a');
    expect(table.textContent).toContain('EQ-1001');
    expect(table.textContent).toContain('FA-2001');
    expect(table.textContent).toContain('chat-1');
    expect(table.textContent).toContain('v3');

    await waitFor(() => expect(urls.length).toBe(1));
    expect(urls[0]).toContain('limit=50');
    expect(urls[0]).toContain('offset=0');
    expect(urls[0]).not.toContain('status=');
    expect(urls[0]).not.toContain('orderBy=');

    const next = screen.getByRole('button', { name: '下一页' });
    expect((next as HTMLButtonElement).disabled).toBe(false);
    await user.click(next);
    await waitFor(() => expect(urls.length).toBe(2));
    expect(urls[1]).toContain('offset=50');

    // 状态下拉 onChange 立即生效，并回到第一页
    await user.selectOptions(screen.getByLabelText('状态'), 'active');
    await waitFor(() => expect(urls.length).toBe(3));
    expect(urls[2]).toContain('status=active');
    expect(urls[2]).toContain('offset=0');

    // 表头排序：未排序 -> desc，且回到第一页
    // （每次刷新都会重新挂载表格，需重新查询当前 DOM 节点）
    await user.click(within(await screen.findByTestId('console-meta-conversations-table')).getByRole('button', { name: '最近消息' }));
    await waitFor(() => expect(urls.length).toBe(4));
    expect(urls[3]).toContain('orderBy=lastMessageAt');
    expect(urls[3]).toContain('order=desc');
    expect(urls[3]).toContain('offset=0');

    // desc -> asc
    await user.click(within(await screen.findByTestId('console-meta-conversations-table')).getByRole('button', { name: '最近消息' }));
    await waitFor(() => expect(urls.length).toBe(5));
    expect(urls[4]).toContain('order=asc');

    // asc -> 清除排序（回到服务端默认）
    await user.click(within(await screen.findByTestId('console-meta-conversations-table')).getByRole('button', { name: '最近消息' }));
    await waitFor(() => expect(urls.length).toBe(6));
    expect(urls[5]).not.toContain('orderBy=');

    // 重置清空全部筛选
    await user.click(screen.getByRole('button', { name: '重置' }));
    await waitFor(() => expect(urls.length).toBe(7));
    expect(urls[6]).not.toContain('status=');
    expect(urls[6]).not.toContain('orderBy=');
  });

  it('renders document metadata with filters, sorting and new columns', async () => {
    const urls: string[] = [];
    server.use(...adminScenario(), metadataSummaryHandler(), documentsMetadataHandler(urls));
    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    await user.click(await screen.findByRole('button', { name: '文件元数据' }));
    const table = await screen.findByTestId('console-meta-documents-table');
    expect(within(table).getAllByRole('row').length).toBe(3); // header + 2 items
    expect(table.textContent).toContain('ext-doc-meta-1');
    expect(table.textContent).toContain('AX-200维修手册.pdf');
    expect(table.textContent).toContain('EAM');
    expect(table.textContent).toContain('manual');
    expect(table.textContent).toContain('ASSET-1');
    expect(table.textContent).toContain('ds-1');
    expect(table.textContent).toContain('未提供'); // 空字段兜底
    // “大小”列默认隐藏（默认精简预设），见下方列开关用例
    expect(table.textContent).not.toContain('12345');

    // 默认预设隐藏“大小”“创建时间”；新增两列可见
    const headers = within(table).getAllByRole('columnheader').map((th) => th.textContent);
    expect(headers).not.toContain('大小');
    expect(headers).not.toContain('创建时间');
    expect(headers).toContain('RAGFlow解析完成');
    expect(headers).toContain('EAM通知时间');

    // 新列取值：有值 vs 未提供
    const rows = within(table).getAllByRole('row');
    const firstCells = within(rows[1]).getAllByRole('cell');
    expect(firstCells.length).toBe(13);
    expect(firstCells[11].textContent).not.toBe('未提供'); // RAGFlow解析完成
    expect(firstCells[12].textContent).not.toBe('未提供'); // EAM通知时间
    const secondCells = within(rows[2]).getAllByRole('cell');
    expect(secondCells.length).toBe(13);
    expect(secondCells[11].textContent).toBe('未提供');
    expect(secondCells[12].textContent).toBe('未提供');

    await waitFor(() => expect(urls.length).toBe(1));
    expect(urls[0]).toContain('limit=50');
    expect(urls[0]).toContain('offset=0');

    // hasMore=false -> 下一页禁用
    expect(((await screen.findByRole('button', { name: '下一页' })) as HTMLButtonElement).disabled).toBe(true);

    await user.type(screen.getByLabelText('来源系统'), 'EAM');
    await user.selectOptions(screen.getByLabelText('同步状态'), 'ready');
    await waitFor(() => expect(urls.length).toBe(2));
    expect(urls[1]).toContain('status=ready');
    expect(urls[1]).toContain('offset=0');

    await user.click(screen.getByRole('button', { name: '筛选' }));
    await waitFor(() => expect(urls.length).toBe(3));
    expect(urls[2]).toContain('sourceSystem=EAM');
    expect(urls[2]).toContain('status=ready');

    // 业务状态筛选（新增）
    await user.selectOptions(screen.getByLabelText('业务状态'), 'review_required');
    await waitFor(() => expect(urls.length).toBe(4));
    expect(urls[3]).toContain('businessStatus=review_required');

    // 新列表头服务端排序（每次刷新都会重新挂载表格，需重新查询当前 DOM 节点）
    await user.click(
      within(await screen.findByTestId('console-meta-documents-table')).getByRole('button', {
        name: 'RAGFlow解析完成',
      }),
    );
    await waitFor(() => expect(urls.length).toBe(5));
    expect(urls[4]).toContain('orderBy=parsedAt');
    expect(urls[4]).toContain('order=desc');

    // 重置清空全部筛选与排序
    await user.click(screen.getByRole('button', { name: '重置' }));
    await waitFor(() => expect(urls.length).toBe(6));
    expect(urls[5]).not.toContain('sourceSystem=');
    expect(urls[5]).not.toContain('status=');
    expect(urls[5]).not.toContain('businessStatus=');
    expect(urls[5]).not.toContain('orderBy=');
  });

  it('renders summary strip chips with quick filters', async () => {
    const convUrls: string[] = [];
    const docUrls: string[] = [];
    server.use(
      ...adminScenario(),
      metadataSummaryHandler(),
      conversationsMetadataHandler(convUrls),
      documentsMetadataHandler(docUrls),
    );
    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    await user.click(await screen.findByRole('button', { name: '文件元数据' }));
    const strip = await screen.findByTestId('console-documents-summary');
    expect(strip.textContent).toContain('文档 13');
    expect(strip.textContent).toContain('ready 9');
    expect(strip.textContent).toContain('failed 4');
    expect(strip.textContent).toContain('review_required 3');

    // 点击 chip = 应用对应同步状态筛选
    await user.click(within(strip).getByRole('button', { name: 'ready 9' }));
    await waitFor(() => expect(docUrls.length).toBe(2));
    expect(docUrls[1]).toContain('status=ready');
    expect(docUrls[1]).toContain('offset=0');
    expect(within(strip).getByRole('button', { name: 'ready 9' }).className).toContain('is-active');

    // 工具栏“已筛选”chip 可单点清除
    await user.click(screen.getByRole('button', { name: '清除同步状态筛选' }));
    await waitFor(() => expect(docUrls.length).toBe(3));
    expect(docUrls[2]).not.toContain('status=');

    await user.click(screen.getByRole('button', { name: '会话元数据' }));
    const convStrip = await screen.findByTestId('console-conversations-summary');
    expect(convStrip.textContent).toContain('会话 3');
    expect(convStrip.textContent).toContain('active 2');
    expect(convStrip.textContent).toContain('archived 1');
    await user.click(within(convStrip).getByRole('button', { name: 'archived 1' }));
    await waitFor(() => expect(convUrls.length).toBe(2));
    expect(convUrls[1]).toContain('status=archived');
    expect(within(convStrip).getByRole('button', { name: 'archived 1' }).className).toContain('is-active');
  });

  it('toggles document columns and persists the selection', async () => {
    server.use(...adminScenario(), metadataSummaryHandler(), documentsMetadataHandler());
    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    await user.click(await screen.findByRole('button', { name: '文件元数据' }));
    const table = await screen.findByTestId('console-meta-documents-table');
    expect(
      within(table).getAllByRole('columnheader').map((th) => th.textContent),
    ).not.toContain('大小');

    await user.click(screen.getByRole('button', { name: '列显示设置' }));
    await user.click(screen.getByRole('checkbox', { name: '大小' }));
    expect(
      within(table).getAllByRole('columnheader').map((th) => th.textContent),
    ).toContain('大小');
    expect(table.textContent).toContain('12345');
    expect(JSON.parse(localStorage.getItem(DOC_HIDDEN_COLUMNS_KEY) ?? '[]')).toEqual(['createdAt']);

    await user.click(screen.getByRole('checkbox', { name: '文件名' }));
    expect(
      within(table).getAllByRole('columnheader').map((th) => th.textContent),
    ).not.toContain('文件名');
    expect(JSON.parse(localStorage.getItem(DOC_HIDDEN_COLUMNS_KEY) ?? '[]')).toEqual([
      'createdAt',
      'fileName',
    ]);

    await user.click(screen.getByRole('button', { name: '恢复默认' }));
    expect(
      within(table).getAllByRole('columnheader').map((th) => th.textContent),
    ).not.toContain('大小');
    expect(JSON.parse(localStorage.getItem(DOC_HIDDEN_COLUMNS_KEY) ?? '[]')).toEqual([
      'sourceSize',
      'createdAt',
    ]);
  });

  it('toggles conversation columns and persists the selection', async () => {
    server.use(...adminScenario(), metadataSummaryHandler(), conversationsMetadataHandler());
    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    await user.click(await screen.findByRole('button', { name: '会话元数据' }));
    const table = await screen.findByTestId('console-meta-conversations-table');
    expect(
      within(table).getAllByRole('columnheader').map((th) => th.textContent),
    ).toContain('设备');

    await user.click(screen.getByRole('button', { name: '列显示设置' }));
    await user.click(screen.getByRole('checkbox', { name: '设备' }));
    expect(
      within(table).getAllByRole('columnheader').map((th) => th.textContent),
    ).not.toContain('设备');
    expect(JSON.parse(localStorage.getItem(CONVERSATION_HIDDEN_COLUMNS_KEY) ?? '[]')).toEqual([
      'equipmentId',
    ]);

    await user.click(screen.getByRole('button', { name: '恢复默认' }));
    expect(
      within(table).getAllByRole('columnheader').map((th) => th.textContent),
    ).toContain('设备');
    expect(JSON.parse(localStorage.getItem(CONVERSATION_HIDDEN_COLUMNS_KEY) ?? '[]')).toEqual([]);
  });

  it('renders conversation admin list and opens the persisted chat', async () => {
    const messageUrls: string[] = [];
    server.use(
      ...adminScenario(),
      metadataSummaryHandler(),
      conversationsMetadataHandler(),
      conversationMessagesHandler({ urls: messageUrls }),
    );
    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    await user.click(await screen.findByRole('button', { name: '会话管理' }));
    const table = await screen.findByTestId('console-admin-conversations-table');
    expect(within(table).getAllByRole('row').length).toBe(3); // header + 2 items
    expect(table.textContent).toContain('conv-meta-1');
    expect(table.textContent).toContain('user-a');
    expect(table.textContent).toContain('查看对话');

    await user.click(within(table).getAllByRole('button', { name: '查看对话' })[0]);
    expect(messageUrls[0]).toContain('/metadata/conversations/conv-meta-1/messages');
    const chat = await screen.findByTestId('console-admin-chat');
    // 用户消息在左（1 条），Gateway 返回 EAM 的回答在右（2 条）
    expect(chat.querySelectorAll('.console-chat-bubble--user').length).toBe(1);
    expect(chat.querySelectorAll('.console-chat-bubble--assistant').length).toBe(2);
    const userBubble = chat.querySelector('.console-chat-bubble--user');
    expect(userBubble?.textContent).toContain('AX-200 报警 E-104');
    expect(userBubble?.querySelector('.console-chat-text')).toBeTruthy();
    // 助手消息走 markdown 渲染
    expect(chat.querySelector('.console-chat-bubble--assistant strong')?.textContent).toBe('液压油位');
    // 持久化业务状态原样映射为中文标签
    expect(chat.textContent).toContain('用户');
    expect(chat.textContent).toContain('EAM 回复');
    expect(chat.textContent).toContain('已完成');
    expect(chat.textContent).toContain('无可靠依据');

    // 会话元信息 chips
    const card = screen.getByTestId('console-admin-conversations-card');
    expect(card.textContent).toContain('业务用户 · user-a');
    expect(card.textContent).toContain('设备 · EQ-1001');
    expect(card.textContent).toContain('固定资产 · FA-2001');
    expect(card.textContent).toContain('context v3');

    await user.click(screen.getByRole('button', { name: '返回列表' }));
    expect(screen.getByTestId('console-admin-conversations-table')).toBeTruthy();
  });

  it('shows conversation admin error and empty message states', async () => {
    server.use(
      ...adminScenario(),
      metadataSummaryHandler(),
      conversationsMetadataHandler(),
      conversationMessagesHandler({ status: 404 }),
    );
    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    await user.click(await screen.findByRole('button', { name: '会话管理' }));
    const table = await screen.findByTestId('console-admin-conversations-table');
    await user.click(within(table).getAllByRole('button', { name: '查看对话' })[0]);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('CONVERSATION_NOT_FOUND');

    // 返回列表不受详情失败影响
    await user.click(screen.getByRole('button', { name: '返回列表' }));
    expect(screen.getByTestId('console-admin-conversations-table')).toBeTruthy();

    // 空消息会话
    server.use(
      conversationMessagesHandler({ body: { conversationId: 'conv-meta-1', items: [] } }),
    );
    await user.click(
      within(screen.getByTestId('console-admin-conversations-table')).getAllByRole('button', {
        name: '查看对话',
      })[0],
    );
    expect(await screen.findByText('该会话暂无持久化消息。')).toBeTruthy();
  });

  it('degrades silently when the metadata summary endpoint fails', async () => {
    server.use(
      ...adminScenario(),
      http.get(
        `${V1}/admin/system/metadata/summary`,
        () =>
          HttpResponse.json(
            {
              code: 'SUMMARY_UNAVAILABLE',
              message: 'summary failed',
              requestId: 'req-summary-500',
              retryable: true,
            },
            { status: 500 },
          ),
      ),
      conversationsMetadataHandler(),
      documentsMetadataHandler(),
    );
    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    await user.click(await screen.findByRole('button', { name: '会话元数据' }));
    expect(await screen.findByTestId('console-meta-conversations-table')).toBeTruthy();
    expect(screen.queryByTestId('console-conversations-summary')).toBeNull();

    await user.click(screen.getByRole('button', { name: '文件元数据' }));
    expect(await screen.findByTestId('console-meta-documents-table')).toBeTruthy();
    expect(screen.queryByTestId('console-documents-summary')).toBeNull();
  });

  it('shows empty states when metadata pages have no rows', async () => {
    server.use(
      ...adminScenario(),
      metadataSummaryHandler(),
      conversationsMetadataHandler(undefined, { items: [], hasMore: false }),
      documentsMetadataHandler(undefined, { items: [], hasMore: false }),
    );
    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    await user.click(await screen.findByRole('button', { name: '会话元数据' }));
    expect(await screen.findByText('暂无会话元数据。')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: '文件元数据' }));
    expect(await screen.findByText('暂无文件元数据。')).toBeTruthy();
  });

  it('isolates a failing integrations module from the metadata panels', async () => {
    server.use(
      ...adminScenario(),
      http.get(
        `${V1}/admin/system/integrations`,
        () =>
          HttpResponse.json(
            {
              code: 'INTEGRATIONS_UNAVAILABLE',
              message: 'integration service exploded',
              requestId: 'req-int-500',
              retryable: true,
            },
            { status: 500 },
          ),
      ),
      conversationsMetadataHandler(),
      metadataSummaryHandler(),
    );
    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    await user.click(await screen.findByRole('button', { name: '接口配置' }));
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('INTEGRATIONS_UNAVAILABLE');
    expect(within(alert).getByRole('button', { name: '重试' })).toBeTruthy();

    // 另一个系统面板照常加载
    await user.click(screen.getByRole('button', { name: '会话元数据' }));
    const table = await screen.findByTestId('console-meta-conversations-table');
    expect(within(table).getAllByRole('row').length).toBe(3);
  });

  it('never renders secrets or bearer material in the console DOM', async () => {
    server.use(
      ...adminScenario(),
      integrationsHandler(),
      metadataSummaryHandler(),
      conversationsMetadataHandler(),
      documentsMetadataHandler(),
    );
    const user = userEvent.setup();
    const view = render(<EnterpriseConsolePage />);

    await user.click(await screen.findByRole('button', { name: '接口配置' }));
    await screen.findByText('https://eam.example');
    const eamProbe = within(within(screen.getByTestId('console-callbacks-table')).getAllByRole('row')[1])
      .getByRole('button', { name: '检测联通' });
    await user.click(eamProbe);
    await screen.findByText(/connected · HTTP 200 · 123ms/);

    const integrationsHtml = view.container.innerHTML;
    expect(integrationsHtml).not.toContain('secret');
    expect(integrationsHtml).not.toContain('hmac');
    expect(integrationsHtml).not.toContain('Bearer ');
    expect(integrationsHtml).not.toContain('SUPERSECRET');

    await user.click(screen.getByRole('button', { name: '文件元数据' }));
    await screen.findByTestId('console-meta-documents-table');
    const documentsHtml = view.container.innerHTML;
    expect(documentsHtml).not.toContain('secret');
    expect(documentsHtml).not.toContain('hmac');
    expect(documentsHtml).not.toContain('Bearer ');
    expect(documentsHtml).not.toContain('SUPERSECRET');
  });
});
