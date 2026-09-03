import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { ConsoleAuthGate } from '../components/layout/ConsoleAuthGate';
import { server } from '../test-setup';

const session = {
  authenticated: true,
  username: 'zkadmin',
  tenantId: 'eam-test-tenant',
  expiresAt: '2030-01-01T00:00:00+00:00',
};

describe('ConsoleAuthGate', () => {
  it('reuses one existing session for protected Console and Harness content', async () => {
    let loginCount = 0;
    server.use(
      http.get('/enterprise/api/v1/console/auth/me', () => HttpResponse.json(session)),
      http.post('/enterprise/api/v1/console/auth/login', async ({ request }) => {
        loginCount += 1;
        const body = await request.json() as { username?: string; password?: string };
        expect(body.username).toBe('zkadmin');
        expect(body.password).toBeTruthy();
        return HttpResponse.json(session);
      }),
    );

    const first = render(
      <ConsoleAuthGate>
        <div data-testid="protected-content">Console and Harness</div>
      </ConsoleAuthGate>,
    );

    expect(await screen.findByTestId('protected-content')).toBeTruthy();
    expect(screen.getByTestId('console-auth-session').textContent).toContain('zkadmin');
    const accountMenu = screen.getByTestId('console-auth-session');
    expect(accountMenu.querySelector('summary')).toBeTruthy();
    await userEvent.setup().click(accountMenu.querySelector('summary') as HTMLElement);
    expect(screen.getByText('租户 · eam-test-tenant')).toBeTruthy();
    expect(screen.getByRole('button', { name: '退出登录' })).toBeTruthy();
    await userEvent.setup().click(screen.getByTestId('protected-content'));
    expect(accountMenu.hasAttribute('open')).toBe(false);
    expect(loginCount).toBe(0);

    first.unmount();
    render(
      <ConsoleAuthGate>
        <div data-testid="protected-content">Console and Harness</div>
      </ConsoleAuthGate>,
    );
    expect(await screen.findByTestId('protected-content')).toBeTruthy();
    expect(loginCount).toBe(0);
  });

  it('prompts for credentials on 401 and does not expose the password after submit', async () => {
    server.use(
      http.get('/enterprise/api/v1/console/auth/me', () => HttpResponse.json(
        { code: 'AUTH_TOKEN_MISSING', message: 'missing', requestId: 'test', retryable: false },
        { status: 401 },
      )),
      http.post('/enterprise/api/v1/console/auth/login', () => HttpResponse.json(session)),
    );

    const user = userEvent.setup();
    render(
      <ConsoleAuthGate>
        <div data-testid="protected-content">Console and Harness</div>
      </ConsoleAuthGate>,
    );

    await screen.findByTestId('console-auth-login');
    const password = screen.getByLabelText('密码') as HTMLInputElement;
    const submittedPassword = crypto.randomUUID();
    await user.type(password, submittedPassword);
    await user.click(screen.getByRole('button', { name: '登录' }));

    await screen.findByTestId('protected-content');
    await waitFor(() => expect(screen.queryByLabelText('密码')).toBeNull());
    expect(screen.queryByText(submittedPassword)).toBeNull();
  });
});
