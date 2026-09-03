import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../App';
import { v2Api } from '../api/v2Client';
import { EnterpriseConsolePage } from '../pages/EnterpriseConsolePage';
import { server } from '../test-setup';

function errorResponse(code: string, message: string) {
  return HttpResponse.json(
    { code, message, requestId: `console-${code}`, retryable: true },
    { status: code.includes('AUTH') ? 401 : 503 },
  );
}

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState({}, '', '/');
});

describe('EnterpriseConsolePage', () => {
  it('is independently reachable at /console without a mode badge in the header', async () => {
    sessionStorage.setItem('enterprise.harness.jwt', 'console-test-token');
    window.history.replaceState({}, '', '/console');

    render(<App />);

    expect(screen.getByTestId('console-page')).toBeTruthy();
    expect(screen.queryByText('TEST · MSW')).toBeNull();
    expect(screen.queryByText('GATEWAY · PUBLIC API')).toBeNull();
    expect(screen.getByRole('link', { name: 'Console' }).getAttribute('aria-current')).toBe('page');
    expect(screen.getByRole('link', { name: 'Harness' }).getAttribute('href')).toBe('/');
    expect(screen.queryByRole('link', { name: '返回 Harness' })).toBeNull();
    const serviceCard = screen.getByTestId('console-service-card');
    expect(serviceCard.textContent).toContain('HMAC secret 不进入浏览器');
    expect(serviceCard.textContent).toContain('sessionStorage 生命周期');
    expect(serviceCard.textContent).not.toContain('console-test-token');
    await screen.findByText(/用户映射：active/);
    // 文档状态面板已移除（用途由系统设置 → 文件元数据覆盖），导航不再提供该入口。
    expect(screen.queryByRole('button', { name: '文档状态' })).toBeNull();
    // 诊断下不再重复提供不可交互的会话历史；Harness 和系统设置分别承载问答与管理。
    expect(screen.queryByRole('button', { name: '会话历史' })).toBeNull();
  });

  it('shows unauthorized modules without exposing credentials', async () => {
    server.use(
      http.get('/enterprise/api/v1/auth/me', () => errorResponse('AUTH_TOKEN_MISSING', 'Authentication token is required')),
      http.get('/enterprise/api/v2/conversations', () => errorResponse('AUTH_TOKEN_INVALID', 'Authentication token is invalid')),
    );

    render(<EnterpriseConsolePage />);

    await waitFor(() => {
      expect(screen.getByTestId('console-service-card').textContent).toContain('unauthorized');
    });
    expect(screen.queryByRole('button', { name: '会话历史' })).toBeNull();
    expect(screen.queryByTestId('console-conversation-card')).toBeNull();
    expect(screen.queryByText(/console-test-token|mock-ticket|cookie\s*[:=]|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/i)).toBeNull();
  });

  it('runs attachment create, ticket and download without rendering ticket or bytes', async () => {
    sessionStorage.setItem('enterprise.harness.jwt', 'console-test-token');
    await v2Api.createConversation({ equipmentId: 'EQ-CONSOLE' });
    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    await user.click(screen.getByRole('button', { name: '临时附件' }));
    const input = await screen.findByLabelText('transient attachment file');
    await waitFor(() => expect((input as HTMLInputElement).disabled).toBe(false));
    await user.upload(input, new File(['console fixture'], 'console.pdf', { type: 'application/pdf' }));
    await user.click(screen.getByRole('button', { name: '通过 Gateway 提交' }));

    await screen.findByText('Gateway 已签发临时附件');
    expect(screen.queryByText(/mock-ticket|downloadUrl|mock attachment bytes/)).toBeNull();
    await user.click(screen.getByRole('button', { name: '验证下载路由' }));
    await screen.findByText(/download route verified/);
    expect(screen.getAllByText('retrievable').length).toBeGreaterThan(0);
  });

  it('keeps runtime credentials out of the Console surface', async () => {
    render(<EnterpriseConsolePage />);

    expect(screen.queryByText('无 Bearer（可测试 401）')).toBeNull();
    expect(screen.queryByLabelText('运行期 Bearer（不写入源码）')).toBeNull();
    expect(screen.queryByRole('button', { name: '保存运行期凭据' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Harness' }).getAttribute('href')).toBe('/');
  });
});
