import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { IntegrationHarnessPage } from '../pages/IntegrationHarnessPage';

describe('IntegrationHarnessPage', () => {
  it('links to /console after JWT can be injected on this page', () => {
    render(<IntegrationHarnessPage />);

    expect((screen.getByLabelText('运行期 Bearer（不写入源码）') as HTMLInputElement).type).toBe('password');
    expect(screen.getByRole('button', { name: '保存运行期凭据' })).toBeTruthy();
    expect(screen.getByRole('link', { name: '打开联调 Console' }).getAttribute('href')).toBe('/console');
  });

  it('keeps the harness reachable on mobile and uses the desktop three-column breakpoint', () => {
    render(<IntegrationHarnessPage />);

    const page = screen.getByTestId('harness-page');
    const layout = screen.getByTestId('harness-layout');
    expect(page.className).toContain('harness-shell');
    expect(page.querySelector('.console-nav')).toBeTruthy();
    expect(layout.className).toContain('harness-layout');
    expect(screen.getByLabelText('功能菜单')).toBeTruthy();
    expect(screen.getByRole('button', { name: '问答会话' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'HTTP 日志' })).toBeTruthy();
    expect(screen.getByLabelText('Asset Registry 设备选择')).toBeTruthy();
    expect(screen.queryByLabelText('transient attachment 边界')).toBeNull();
    expect(screen.queryByLabelText('运行')).toBeNull();
  });

  it('shows only the selected menu panel', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);

    expect(screen.getByTestId('harness-layout')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'HTTP 日志' }));
    expect(screen.queryByTestId('harness-layout')).toBeNull();
    expect(screen.getByTestId('harness-runtime-log')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: '临时附件' }));
    expect(screen.queryByTestId('harness-runtime-log')).toBeNull();
    expect(screen.getByLabelText('transient attachment 边界')).toBeTruthy();
  });

  it('guides local Asset Registry keys when creating a conversation', () => {
    render(<IntegrationHarnessPage />);

    expect(screen.getByText(/本地联调请使用/)).toBeTruthy();
    expect(screen.getByPlaceholderText('equipmentId，例如 EQ-GD01250002')).toBeTruthy();
    expect(screen.getByPlaceholderText('fixedAssetNo，例如 GD01250002')).toBeTruthy();
    expect(screen.getByPlaceholderText('faultCode，例如 E-104')).toBeTruthy();
  });

  it('replays file event, document polling, and context switch scenarios', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);

    await user.click(screen.getByRole('button', { name: '文档' }));
    await user.click(screen.getByRole('button', { name: '提交文件事件' }));
    await screen.findByText('未声明 ready（received）');
    expect(screen.getByText('文档状态与质量诊断')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: '问答会话' }));
    await user.click(screen.getByRole('button', { name: '创建并选择' }));
    await screen.findAllByText('Harness 会话');
    expect(await screen.findByText('FA-2001')).toBeTruthy();
    const equipment = screen.getByLabelText('equipmentId');
    await user.clear(equipment);
    await user.type(equipment, 'EQ-1002');
    await user.click(screen.getByRole('button', { name: '切换 Asset context' }));
    await waitFor(() => expect(screen.getByText(/contextVersion:/)).toBeTruthy());
  });

  it('renders independent business status and citation evidence from SSE', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    await user.click(screen.getByRole('button', { name: '创建并选择' }));
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
    await user.click(screen.getByRole('button', { name: '创建并选择' }));
    const input = await screen.findByLabelText('问题输入');
    await user.type(input, 'noevidence');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText('业务状态：无可靠依据');
    expect(screen.getByText('citations: 0')).toBeTruthy();
  });

  it('preserves failed status and citations when the conversation is replayed', async () => {
    const user = userEvent.setup();
    const first = render(<IntegrationHarnessPage />);
    await user.click(screen.getByRole('button', { name: '创建并选择' }));
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
    const equipment = screen.getByLabelText('new equipmentId');
    await user.clear(equipment);
    await user.type(equipment, `scenario-${scenario}`);
    await user.click(screen.getByRole('button', { name: '创建并选择' }));
    await screen.findByText(new RegExp(`\\[${code}\\]`));
    expect(screen.getByText(title)).toBeTruthy();
  });

  it('shows Asset Registry unavailable without falling back to an unscoped conversation', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    const equipment = screen.getByLabelText('new equipmentId');
    await user.clear(equipment);
    await user.type(equipment, 'asset-registry');
    await user.click(screen.getByRole('button', { name: '创建并选择' }));
    await screen.findByText(/\[ASSET_REGISTRY_UNAVAILABLE\]/);
    expect(screen.getByText('依赖服务暂时不可用（503）')).toBeTruthy();
    expect(screen.queryByText('Gateway 已返回的 Asset Registry snapshot')).toBeNull();
  });

  it('shows gateway HTTP request and response logs in the runtime panel', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    await user.click(screen.getByRole('button', { name: 'HTTP 日志' }));
    const panel = screen.getByTestId('harness-runtime-log');
    expect(panel.textContent).toContain('运行');
    await screen.findByText('/enterprise/api/v2/conversations');
    expect(panel.textContent).toContain('POST');
    expect(panel.textContent).toContain('/enterprise/api/v3/documents');
    expect(panel.textContent).not.toContain('should-not-appear');
  });

  it('shows the transient attachment expiry returned by Gateway', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    await user.click(screen.getByRole('button', { name: '创建并选择' }));
    await user.click(screen.getByRole('button', { name: '临时附件' }));
    const input = await screen.findByLabelText('transient attachment file');
    await waitFor(() => expect((input as HTMLInputElement).disabled).toBe(false));
    const file = new File(['expired attachment'], 'expired-manual.pdf', { type: 'application/pdf' });
    await user.upload(input, file);
    const uploadButton = screen.getByRole('button', { name: '通过 Gateway 提交' });
    await waitFor(() => expect((uploadButton as HTMLButtonElement).disabled).toBe(false));
    await user.click(uploadButton);
    const attachmentError = await screen.findByRole('alert');
    expect(attachmentError.textContent).toContain('ATTACHMENT_EXPIRED');
    expect(attachmentError.textContent).toContain('HTTP 410');
  });
});
