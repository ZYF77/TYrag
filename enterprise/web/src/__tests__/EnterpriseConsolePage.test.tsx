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
    await screen.findByText('FILE-SHARE-READY');
    await screen.findByText(/用户映射：active/);
  });

  it('isolates a FILE_SHARE outage while service and session cards remain', async () => {
    sessionStorage.setItem('enterprise.harness.jwt', 'console-test-token');
    server.use(
      http.get('/enterprise/api/v3/documents/sync-status', () =>
        errorResponse('FILE_STATUS_UNAVAILABLE', 'FILE_SHARE status unavailable'),
      ),
    );

    render(<EnterpriseConsolePage />);

    await screen.findByText(/FILE_STATUS_UNAVAILABLE/);
    expect(screen.getByTestId('console-service-card')).toBeTruthy();
    expect(screen.getByTestId('console-conversation-card')).toBeTruthy();
    expect(screen.getByText('Gateway liveness')).toBeTruthy();
  });

  it('shows unauthorized modules without exposing credentials', async () => {
    server.use(
      http.get('/enterprise/api/v1/auth/me', () => errorResponse('AUTH_TOKEN_MISSING', 'Authentication token is required')),
      http.get('/enterprise/api/v3/documents/sync-status', () => errorResponse('AUTH_HMAC_REQUIRED', 'HMAC producer required')),
      http.get('/enterprise/api/v2/conversations', () => errorResponse('AUTH_TOKEN_INVALID', 'Authentication token is invalid')),
    );

    render(<EnterpriseConsolePage />);

    await waitFor(() => {
      expect(screen.getByTestId('console-service-card').textContent).toContain('unauthorized');
      expect(screen.getByTestId('console-document-card').textContent).toContain('unauthorized');
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

    await user.click(screen.getByRole('button', { name: '新建诊断会话' }));
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
});
