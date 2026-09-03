import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { IntegrationHarnessPage } from '../pages/IntegrationHarnessPage';
import { server } from '../test-setup';

type User = ReturnType<typeof userEvent.setup>;

async function createConversationWithoutDevice(user: User) {
  const button = await screen.findByRole('button', { name: '+ 新建会话' });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  await user.click(button);
}

describe('IntegrationHarnessPage', () => {
  it('links to /console without exposing runtime credentials on the Harness page', () => {
    render(<IntegrationHarnessPage />);

    expect(screen.queryByLabelText('运行期 Bearer（不写入源码）')).toBeNull();
    expect(screen.queryByRole('button', { name: '保存运行期凭据' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Console' }).getAttribute('href')).toBe('/console');
  });

  it('keeps the harness reachable on mobile and uses the desktop two-column breakpoint', async () => {
    render(<IntegrationHarnessPage />);

    const page = screen.getByTestId('harness-page');
    const layout = screen.getByTestId('harness-layout');
    expect(page.className).toContain('harness-shell');
    expect(page.querySelector('.console-nav')).toBeTruthy();
    expect(layout.className).toContain('harness-layout');
    expect(screen.getByLabelText('功能菜单')).toBeTruthy();
    expect(screen.getByRole('button', { name: '问答会话' })).toBeTruthy();
    await userEvent.setup().click(screen.getByRole('button', { name: '运行' }));
    expect(screen.getByRole('button', { name: 'HTTP 日志' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '临时附件' })).toBeNull();
    expect(screen.queryByRole('button', { name: '文档' })).toBeNull();
    expect(screen.getByLabelText('会话管理')).toBeTruthy();
    expect(screen.getByRole('button', { name: '+ 新建会话' })).toBeTruthy();
    expect(screen.getByText('external contract v2.9.0')).toBeTruthy();
  });

  it('collapses navigation groups until their header is opened', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);

    const runtimeGroup = screen.getByRole('button', { name: '运行' });
    expect(runtimeGroup.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('button', { name: 'HTTP 日志' })).toBeNull();
    await user.click(runtimeGroup);
    expect(runtimeGroup.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('button', { name: 'HTTP 日志' })).toBeTruthy();
    await user.click(runtimeGroup);
    expect(screen.queryByRole('button', { name: 'HTTP 日志' })).toBeNull();
  });

  it('shows only the selected menu panel', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);

    expect(screen.getByTestId('harness-layout')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: '运行' }));
    await user.click(screen.getByRole('button', { name: 'HTTP 日志' }));
    expect(screen.queryByTestId('harness-layout')).toBeNull();
    expect(screen.getByTestId('harness-runtime-log')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: '问答会话' }));
    expect(screen.queryByTestId('harness-runtime-log')).toBeNull();
    expect(screen.getByTestId('harness-layout')).toBeTruthy();
  });

  it('creates a device-less conversation and offers optional device creation', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);

    await createConversationWithoutDevice(user);
    expect((await screen.findAllByText('设备: 未绑定设备')).length).toBeGreaterThanOrEqual(1);

    await user.click(screen.getByRole('button', { name: '指定设备创建（可选）' }));
    expect(screen.getByRole('dialog', { name: '指定设备创建' })).toBeTruthy();
    const equipment = screen.getByLabelText('new equipmentId');
    expect((equipment as HTMLInputElement).value).toBe('');
    expect(screen.getByText(/设备号可留空/)).toBeTruthy();
    expect(screen.queryByLabelText('new faultCode')).toBeNull();

    await user.type(equipment, 'EQ-1001');
    await user.click(screen.getByRole('button', { name: '创建会话' }));
    expect((await screen.findAllByText('设备: EQ-1001')).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByRole('dialog', { name: '指定设备创建' })).toBeNull();
  });

  it('rebinds the device through the session header inline form', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);

    await createConversationWithoutDevice(user);
    await screen.findAllByText('设备: 未绑定设备');

    expect(screen.getByRole('button', { name: '复制会话 ID' })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: '换绑设备' }));
    const equipment = screen.getByLabelText('equipmentId');
    await user.clear(equipment);
    await user.type(equipment, 'EQ-1002');
    await user.click(screen.getByRole('button', { name: '保存换绑' }));

    await waitFor(() => expect(screen.getByTestId('harness-device-badge').textContent).toBe('设备: EQ-1002'));
    expect(screen.queryByText(/canonical snapshot/i)).toBeNull();
  });

  it('clears the composer draft when starting a new conversation', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);

    await createConversationWithoutDevice(user);
    const input = await screen.findByLabelText('问题输入') as HTMLTextAreaElement;
    await user.type(input, '只属于旧会话的草稿');
    await user.click(screen.getByRole('button', { name: '+ 新建会话' }));

    await waitFor(() => expect(input.value).toBe(''));
  });

  it('copies the active conversation id from the session metadata strip', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    render(<IntegrationHarnessPage />);

    await createConversationWithoutDevice(user);
    await user.click(screen.getByRole('button', { name: '复制会话 ID' }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(expect.stringMatching(/^v2-conv-/)));
  });

  it('renders independent business status and citation evidence from SSE', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    await createConversationWithoutDevice(user);
    const input = await screen.findByLabelText('问题输入');
    await user.type(input, 'how to inspect?');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText(/Harness answer/);
    await screen.findByText('业务状态：已完成');
    expect(screen.getByText('引用 1 条')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: /Harness maintenance manual/ }));
    await screen.findByText('引用详情');
    expect(screen.getAllByText('externalDocumentId').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('assetId').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('ASSET-HARNESS-001').length).toBeGreaterThanOrEqual(1);
  });

  it('sends the selected reasoning level and internet preference to Gateway v2', async () => {
    const user = userEvent.setup();
    let received: Record<string, unknown> | null = null;
    server.use(
      http.post('/enterprise/api/v2/conversations/:conversationId/messages', async ({ request }) => {
        received = await request.json() as Record<string, unknown>;
        const payload = [
          `event: run.started\ndata: ${JSON.stringify({ runId: 'run-settings' })}\n\n`,
          `event: answer.delta\ndata: ${JSON.stringify({ content: 'Gateway payload accepted' })}\n\n`,
          `event: answer.completed\ndata: ${JSON.stringify({ status: '已完成', citations: [] })}\n\n`,
        ].join('');
        return new HttpResponse(payload, { headers: { 'Content-Type': 'text/event-stream' } });
      }),
    );

    render(<IntegrationHarnessPage />);
    await createConversationWithoutDevice(user);
    await user.selectOptions(screen.getByLabelText('推理档位'), 'high');
    await user.click(screen.getByLabelText('联网检索'));
    await user.type(screen.getByLabelText('问题输入'), 'settings payload');
    await user.click(screen.getByRole('button', { name: '发送' }));

    await screen.findByText('Gateway payload accepted');
    expect(received).toMatchObject({ reasoningMode: 'high', internetEnabled: true });
  });

  it('renders streamed reasoning separately and keeps it out of the answer body', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    await createConversationWithoutDevice(user);
    await user.type(screen.getByLabelText('问题输入'), 'show reasoning stages');
    await user.click(screen.getByRole('button', { name: '发送' }));

    await screen.findByText(/Harness answer: show reasoning stages/);
    const disclosure = await screen.findByRole('button', { name: /思考过程/ });
    expect(disclosure.getAttribute('aria-expanded')).toBe('false');
    await user.click(disclosure);
    expect(screen.getByLabelText('思考过程').textContent).toContain('已完成范围确认');
    expect(screen.getByText(/Harness answer: show reasoning stages/)).toBeTruthy();
  });

  it('shows no-reliable-evidence independently of citation count', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    await createConversationWithoutDevice(user);
    const input = await screen.findByLabelText('问题输入');
    await user.type(input, 'noevidence');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText('业务状态：无可靠依据');
    expect(screen.getByText('引用 0 条')).toBeTruthy();
  });

  it('preserves failed status and citations when the conversation is replayed', async () => {
    const user = userEvent.setup();
    const first = render(<IntegrationHarnessPage />);
    await createConversationWithoutDevice(user);
    const input = await screen.findByLabelText('问题输入');
    await user.type(input, 'sse-error replay status');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText('业务状态：失败');

    first.unmount();
    render(<IntegrationHarnessPage />);
    await user.click(await screen.findByRole('button', { name: /sse-error replay status/ }));
    await screen.findByText('业务状态：失败');
    expect(screen.getByText('引用 1 条')).toBeTruthy();
    expect(screen.getByText(/ASSET-HARNESS-001/)).toBeTruthy();
  });

  it.each([
    ['401', 'AUTH_TOKEN_INVALID', '登录已过期'],
    ['403', 'ACL_DENIED', '权限不足'],
    ['409', 'CONVERSATION_CONTEXT_STALE', '请求冲突（409）'],
    ['503', 'RAGFLOW_UNAVAILABLE', '服务暂时不可用'],
  ])('renders the Gateway %s permission/dependency boundary', async (scenario, code, title) => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    await user.click(screen.getByRole('button', { name: '指定设备创建（可选）' }));
    const equipment = screen.getByLabelText('new equipmentId');
    await user.clear(equipment);
    await user.type(equipment, `scenario-${scenario}`);
    await user.click(screen.getByRole('button', { name: '创建会话' }));
    await screen.findByText(new RegExp(`\\[${code}\\]`));
    expect(screen.getByText(title)).toBeTruthy();
  });

  it('creates conversations without an Asset Registry fallback when the dependency is down', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    await user.click(screen.getByRole('button', { name: '指定设备创建（可选）' }));
    await user.type(screen.getByLabelText('new equipmentId'), 'asset-registry');
    await user.click(screen.getByRole('button', { name: '创建会话' }));
    await screen.findByText(/\[ASSET_REGISTRY_UNAVAILABLE\]/);
    expect(screen.getByText('依赖服务暂时不可用（503）')).toBeTruthy();
    expect(screen.queryByLabelText('Asset Registry 设备选择')).toBeNull();
  });

  it('stages question files with the paperclip and streams them via multipart', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    await createConversationWithoutDevice(user);
    const input = await screen.findByLabelText('问题输入');
    const fileInput = screen.getByLabelText('选择附件');
    expect(screen.getByRole('button', { name: '添加附件' })).toBeTruthy();

    await user.upload(fileInput, new File(['hello'], 'note.txt', { type: 'text/plain' }));
    expect(screen.getByText('note.txt')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: '移除附件 note.txt' }));
    expect(screen.queryByText('note.txt')).toBeNull();

    await user.upload(fileInput, new File(['hello'], 'note.txt', { type: 'text/plain' }));
    await user.type(input, 'how to inspect?');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText(/Harness answer/);
    // staged chip is cleared after send; the user bubble echoes the file name
    expect(screen.getAllByText('note.txt').length).toBeGreaterThanOrEqual(1);
  });

  it('shows gateway HTTP request and response logs in the runtime panel', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    await user.click(screen.getByRole('button', { name: '运行' }));
    await user.click(screen.getByRole('button', { name: 'HTTP 日志' }));
    const panel = screen.getByTestId('harness-runtime-log');
    expect(screen.getByTestId('workbench-active-title').textContent).toBe('HTTP 日志');
    expect(panel.textContent).toContain('运行');
    await screen.findByRole('button', { name: /\/enterprise\/api\/v2\/conversations\/conv-test\/messages/ });
    expect(panel.textContent).toContain('POST');
    expect(panel.textContent).toContain('/enterprise/api/v3/documents');
    expect(screen.getByTestId('runtime-total').textContent).toBe('3');
    expect(screen.getByTestId('runtime-failures').textContent).toBe('1');
    expect(screen.getByLabelText('接口类型')).toBeTruthy();
    expect(screen.getByLabelText('业务场景')).toBeTruthy();
    expect(screen.getByLabelText('HTTP 方法')).toBeTruthy();
    expect(screen.getByLabelText('失败原因或故障码')).toBeTruthy();
    expect(screen.getByLabelText('请求方、调用方或用户名')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: '时间范围' }));
    expect(screen.getByLabelText('开始时间').getAttribute('type')).toBe('datetime-local');
    expect(screen.getByLabelText('结束时间').getAttribute('type')).toBe('datetime-local');
    await user.click(await screen.findByRole('button', { name: '关闭时间范围' }));
    expect(panel.textContent).toContain('demo-user');

    await user.click(screen.getByRole('button', { name: /\/enterprise\/api\/v2\/conversations\/conv-test\/messages/ }));
    expect(screen.getByTestId('runtime-detail-request').textContent).toContain('请求');
    expect(screen.getByTestId('runtime-detail-response').textContent).toContain('VALIDATION_ERROR');
    expect(screen.getByTestId('runtime-detail-meta').textContent).toContain('inquiry.http');
    await user.click(screen.getByRole('button', { name: '关闭 HTTP 日志详情' }));

    await user.selectOptions(screen.getByLabelText('业务场景'), 'feed');
    expect(screen.getByTestId('runtime-total').textContent).toBe('1');
    expect(screen.queryByText('/enterprise/api/v2/conversations')).toBeNull();
    await user.click(screen.getByRole('button', { name: '重置筛选' }));
    await user.type(screen.getByLabelText('失败原因或故障码'), 'VALIDATION_ERROR');
    expect(screen.getByTestId('runtime-total').textContent).toBe('1');
    await user.click(screen.getByRole('button', { name: /\/enterprise\/api\/v2\/conversations\/conv-test\/messages/ }));
    expect(screen.getByTestId('runtime-detail-request').textContent).toContain('/enterprise/api/v2/conversations/conv-test/messages');
    expect(panel.textContent).not.toContain('should-not-appear');
  });

  it('classifies admin routes separately and keeps the request caller visible', async () => {
    server.use(
      http.get('/enterprise/api/v1/diagnostics/http-log', () => HttpResponse.json({
        items: [{
          id: 'admin-1',
          ts: '2026-09-02T06:31:30.000Z',
          direction: 'inbound',
          kind: 'http',
          method: 'GET',
          path: '/enterprise/api/v1/admin/system/metadata/documents',
          query: 'limit=50&offset=0',
          caller: 'console-web',
          caller_username: 'zkadmin',
          http_status: 200,
          duration_ms: 6,
          body: null,
          response_body: { items: [] },
          streamed: false,
        }],
      })),
    );
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    await user.click(screen.getByRole('button', { name: '运行' }));
    await user.click(screen.getByRole('button', { name: 'HTTP 日志' }));

    const panel = screen.getByTestId('harness-runtime-log');
    expect(within(panel).getByText('系统管理', { selector: 'b' })).toBeTruthy();
    expect(panel.textContent).toContain('管理接口');
    expect(panel.textContent).toContain('zkadmin · console-web');
    expect(panel.textContent).toContain('limit=50&offset=0');
    await user.click(screen.getByRole('button', { name: /metadata\/documents/ }));
    expect(screen.getByTestId('runtime-detail-request').textContent).toContain('请求体');
    expect(screen.getByTestId('runtime-detail-request').textContent).toContain('无（GET/HEAD 请求无请求体）');
  });

  it('pages filtered runtime logs while keeping newest requests first', async () => {
    server.use(
      http.get('/enterprise/api/v1/diagnostics/http-log', () => HttpResponse.json({
        items: Array.from({ length: 21 }, (_, index) => ({
          id: String(21 - index),
          ts: new Date(Date.now() - index * 1_000).toISOString(),
          direction: 'inbound',
          kind: 'http',
          method: 'GET',
          path: `/runtime/${index}`,
          query: '',
          caller: 'local-test',
          http_status: 200,
          duration_ms: index,
          body: null,
          response_body: { ok: true },
          streamed: false,
        })),
      })),
    );
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    await user.click(screen.getByRole('button', { name: '运行' }));
    await user.click(screen.getByRole('button', { name: 'HTTP 日志' }));
    await screen.findByText('/runtime/0');
    await user.selectOptions(screen.getByLabelText('每页大小'), '20');
    expect(screen.queryByText('/runtime/20')).toBeNull();
    await user.click(screen.getByRole('button', { name: '下一页' }));
    expect(await screen.findByText('/runtime/20')).toBeTruthy();
    expect(screen.queryByText('/runtime/0')).toBeNull();
  });
});
