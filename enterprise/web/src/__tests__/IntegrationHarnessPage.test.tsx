import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { IntegrationHarnessPage } from '../pages/IntegrationHarnessPage';
import { server } from '../test-setup';

type User = ReturnType<typeof userEvent.setup>;

async function createConversationWithoutDevice(user: User) {
  const button = await screen.findByRole('button', { name: '+ 新建会话' });
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  await user.click(button);
}

describe('IntegrationHarnessPage', () => {
  it('links to /console after JWT can be injected on this page', () => {
    render(<IntegrationHarnessPage />);

    expect((screen.getByLabelText('运行期 Bearer（不写入源码）') as HTMLInputElement).type).toBe('password');
    expect(screen.getByRole('button', { name: '保存运行期凭据' })).toBeTruthy();
    expect(screen.getByRole('link', { name: '打开联调 Console' }).getAttribute('href')).toBe('/console');
  });

  it('keeps the harness reachable on mobile and uses the desktop two-column breakpoint', () => {
    render(<IntegrationHarnessPage />);

    const page = screen.getByTestId('harness-page');
    const layout = screen.getByTestId('harness-layout');
    expect(page.className).toContain('harness-shell');
    expect(page.querySelector('.console-nav')).toBeTruthy();
    expect(layout.className).toContain('harness-layout');
    expect(screen.getByLabelText('功能菜单')).toBeTruthy();
    expect(screen.getByRole('button', { name: '问答会话' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'HTTP 日志' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '临时附件' })).toBeNull();
    expect(screen.queryByRole('button', { name: '文档' })).toBeNull();
    expect(screen.getByLabelText('会话管理')).toBeTruthy();
    expect(screen.getByRole('button', { name: '+ 新建会话' })).toBeTruthy();
    expect(screen.getByText('external contract v2.9.0')).toBeTruthy();
  });

  it('shows only the selected menu panel', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);

    expect(screen.getByTestId('harness-layout')).toBeTruthy();
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
    const equipment = screen.getByLabelText('new equipmentId');
    expect((equipment as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText('new fixedAssetNo') as HTMLInputElement).value).toBe('');
    expect(screen.getByText(/设备号可留空/)).toBeTruthy();
    expect(screen.queryByLabelText('new faultCode')).toBeNull();

    await user.type(equipment, 'EQ-1001');
    await user.type(screen.getByLabelText('new fixedAssetNo'), 'FA-2001');
    await user.click(screen.getByRole('button', { name: '创建会话' }));
    expect(await screen.findByText('设备: EQ-1001 · FA-2001')).toBeTruthy();
  });

  it('rebinds the device through the session header inline form', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);

    await createConversationWithoutDevice(user);
    await screen.findAllByText('设备: 未绑定设备');

    await user.click(screen.getByRole('button', { name: '换绑' }));
    const equipment = screen.getByLabelText('equipmentId');
    await user.clear(equipment);
    await user.type(equipment, 'EQ-1002');
    await user.click(screen.getByRole('button', { name: '保存换绑' }));

    await waitFor(() => expect(screen.getByTestId('harness-device-badge').textContent).toBe('设备: EQ-1002'));
    expect(screen.queryByText(/canonical snapshot/i)).toBeNull();
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
    expect(screen.getByText(/citations: 1/)).toBeTruthy();

    await user.click(screen.getByRole('button', { name: /Harness maintenance manual/ }));
    await screen.findByText('Citation snapshot');
    expect(screen.getAllByText('externalDocumentId').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('assetId').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('ASSET-HARNESS-001').length).toBeGreaterThanOrEqual(1);
  });

  it('shows no-reliable-evidence independently of citation count', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    await createConversationWithoutDevice(user);
    const input = await screen.findByLabelText('问题输入');
    await user.type(input, 'noevidence');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText('业务状态：无可靠依据');
    expect(screen.getByText('citations: 0')).toBeTruthy();
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
    expect(screen.getByText('citations: 1')).toBeTruthy();
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
    await user.click(screen.getByRole('button', { name: 'HTTP 日志' }));
    const panel = screen.getByTestId('harness-runtime-log');
    expect(screen.getByTestId('workbench-active-title').textContent).toBe('HTTP 日志');
    expect(panel.textContent).toContain('运行');
    await screen.findByText('/enterprise/api/v2/conversations');
    expect(panel.textContent).toContain('POST');
    expect(panel.textContent).toContain('/enterprise/api/v3/documents');
    expect(screen.getByTestId('runtime-total').textContent).toBe('3');
    expect(screen.getByTestId('runtime-failures').textContent).toBe('1');
    expect(screen.getByLabelText('接口类型')).toBeTruthy();
    expect(screen.getByLabelText('业务场景')).toBeTruthy();
    expect(screen.getByLabelText('HTTP 方法')).toBeTruthy();
    expect(screen.getByLabelText('失败原因或故障码')).toBeTruthy();
    expect(screen.getByLabelText('请求方或调用方')).toBeTruthy();

    await user.selectOptions(screen.getByLabelText('业务场景'), 'feed');
    expect(screen.getByTestId('runtime-total').textContent).toBe('1');
    expect(screen.queryByText('/enterprise/api/v2/conversations')).toBeNull();
    await user.click(screen.getByRole('button', { name: '重置筛选' }));
    await user.type(screen.getByLabelText('失败原因或故障码'), 'VALIDATION_ERROR');
    expect(screen.getByTestId('runtime-total').textContent).toBe('1');
    expect(screen.getByText('/enterprise/api/v2/conversations/conv-test/messages')).toBeTruthy();
    expect(panel.textContent).not.toContain('should-not-appear');
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
    await user.click(screen.getByRole('button', { name: 'HTTP 日志' }));
    await screen.findByText('/runtime/0');
    await user.selectOptions(screen.getByLabelText('每页条数'), '10');
    expect(screen.queryByText('/runtime/10')).toBeNull();
    await user.click(screen.getByRole('button', { name: '下一页' }));
    expect(await screen.findByText('/runtime/10')).toBeTruthy();
    expect(screen.queryByText('/runtime/0')).toBeNull();
  });
});
