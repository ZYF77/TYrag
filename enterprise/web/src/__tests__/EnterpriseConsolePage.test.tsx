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
  it('is independently reachable at /console and labels the MSW boundary', async () => {
    sessionStorage.setItem('enterprise.harness.jwt', 'console-test-token');
    window.history.replaceState({}, '', '/console');

    render(<App />);

    expect(screen.getByTestId('console-page')).toBeTruthy();
    expect(screen.getByText('TEST · MSW')).toBeTruthy();
    const serviceCard = screen.getByTestId('console-service-card');
    expect(serviceCard.textContent).toContain('HMAC secret 不进入浏览器');
    expect(serviceCard.textContent).toContain('sessionStorage 生命周期');
    expect(serviceCard.textContent).not.toContain('console-test-token');
    await screen.findByText(/用户映射：active/);
    // 文档状态面板已移除（用途由系统设置 → 文件元数据覆盖），导航不再提供该入口。
    expect(screen.queryByRole('button', { name: '文档状态' })).toBeNull();
  });

  it('shows unauthorized modules without exposing credentials', async () => {
    server.use(
      http.get('/enterprise/api/v1/auth/me', () => errorResponse('AUTH_TOKEN_MISSING', 'Authentication token is required')),
      http.get('/enterprise/api/v2/conversations', () => errorResponse('AUTH_TOKEN_INVALID', 'Authentication token is invalid')),
    );

    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    await waitFor(() => {
      expect(screen.getByTestId('console-service-card').textContent).toContain('unauthorized');
    });
    await user.click(screen.getByRole('button', { name: '会话历史' }));
    await waitFor(() => {
      expect(screen.getByTestId('console-conversation-card').textContent).toContain('unauthorized');
    });
    expect(screen.queryByText(/console-test-token|mock-ticket|cookie\s*[:=]|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/i)).toBeNull();
  });

  it('loads persisted history and re-authorizes a citation snapshot independently', async () => {
    sessionStorage.setItem('enterprise.harness.jwt', 'console-test-token');
    const conversation = await v2Api.createConversation({ equipmentId: 'EQ-CONSOLE' });
    const stream = v2Api.streamMessage(
      conversation.conversationId,
      { clientMessageId: 'console-history-message', question: 'console citation trace' },
      () => undefined,
    );
    await stream.promise;

    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);
    await user.click(screen.getByRole('button', { name: '会话历史' }));
    await user.click(screen.getByRole('button', { name: '刷新会话列表' }));
    const session = await screen.findByRole('button', { name: /console citation trace/ });
    await user.click(session);
    await screen.findByText(/citations 1/);
    await user.click(screen.getByRole('button', { name: /citation 1/ }));
    await screen.findByText('Harness maintenance manual');
    expect(screen.queryByText(/Harness answer: console citation trace/)).toBeNull();
  });

  it('runs attachment create, ticket and download without rendering ticket or bytes', async () => {
    sessionStorage.setItem('enterprise.harness.jwt', 'console-test-token');
    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    await user.click(screen.getByRole('button', { name: '会话历史' }));
    await user.click(screen.getByRole('button', { name: '新建诊断会话' }));
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

  it('injects runtime Bearer without displaying the token and refreshes identity', async () => {
    const user = userEvent.setup();
    render(<EnterpriseConsolePage />);

    expect(screen.getByText('无 Bearer（可测试 401）')).toBeTruthy();
    const input = screen.getByLabelText('运行期 Bearer（不写入源码）');
    expect((input as HTMLInputElement).type).toBe('password');
    expect(screen.getByRole('link', { name: '返回 Harness' }).getAttribute('href')).toBe('/');

    await user.type(input, 'console-test-token');
    await user.click(screen.getByRole('button', { name: '保存运行期凭据' }));

    expect(sessionStorage.getItem('enterprise.harness.jwt')).toBe('console-test-token');
    expect((input as HTMLInputElement).value).toBe('');
    expect(screen.getByText('Bearer 已注入')).toBeTruthy();
    expect(screen.queryByText('console-test-token')).toBeNull();
    await screen.findByText(/用户映射：active/);
  });
});
