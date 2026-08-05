import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ErrorBanner } from '../components/errors/ErrorBanner';

describe('ErrorBanner', () => {
  it('renders 401 auth error with login prompt', () => {
    render(
      <ErrorBanner
        error={{
          code: 'AUTH_TOKEN_INVALID',
          message: '登录已过期，请重新登录',
          requestId: 'req-auth-001',
        }}
      />,
    );

    expect(screen.getByText('登录已过期')).toBeTruthy();
    expect(screen.getAllByText(/请重新登录/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/req-auth-001/)).toBeTruthy();
    expect(screen.getByRole('alert')).toBeTruthy();
  });

  it('renders 403 permission error with security message', () => {
    render(
      <ErrorBanner
        error={{
          code: 'ACL_DENIED',
          message: '您没有权限访问此资源',
          requestId: 'req-acl-001',
        }}
      />,
    );

    expect(screen.getByText('权限不足')).toBeTruthy();
    expect(screen.getAllByText(/没有权限/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/联系管理员/)).toBeTruthy();
  });

  it('renders 404 not found error', () => {
    render(
      <ErrorBanner
        error={{
          code: 'CONVERSATION_NOT_FOUND',
          message: '会话不存在',
          requestId: 'req-404-001',
        }}
      />,
    );

    expect(screen.getAllByText('会话不存在').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/已被删除/)).toBeTruthy();
  });

  it('renders service unavailable (degraded) error', () => {
    render(
      <ErrorBanner
        error={{
          code: 'RAGFLOW_UNAVAILABLE',
          message: '知识库服务暂时不可用',
        }}
      />,
    );

    expect(screen.getByText('服务暂时不可用')).toBeTruthy();
    expect(screen.getByText(/部分功能降级/)).toBeTruthy();
  });

  it('renders generic error for unknown codes', () => {
    render(
      <ErrorBanner
        error={{
          code: 'UNKNOWN_ERROR',
          message: '发生了未知错误',
        }}
      />,
    );

    expect(screen.getByText('请求失败')).toBeTruthy();
    expect(screen.getByText('发生了未知错误')).toBeTruthy();
  });

  it('can be dismissed', async () => {
    let dismissed = false;
    render(
      <ErrorBanner
        error={{
          code: 'AUTH_TOKEN_INVALID',
          message: 'test',
        }}
        onDismiss={() => {
          dismissed = true;
        }}
      />,
    );

    const closeBtn = screen.getByLabelText('关闭错误提示');
    await userEvent.click(closeBtn);

    expect(dismissed).toBe(true);
  });

  it('disappears after dismiss', async () => {
    render(
      <ErrorBanner
        error={{
          code: 'AUTH_TOKEN_INVALID',
          message: 'test',
        }}
      />,
    );

    expect(screen.getByRole('alert')).toBeTruthy();

    const closeBtn = screen.getByLabelText('关闭错误提示');
    await userEvent.click(closeBtn);

    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('reappears when a new error arrives after dismiss', async () => {
    const { rerender } = render(
      <ErrorBanner
        error={{
          code: 'AUTH_TOKEN_INVALID',
          message: 'auth error',
        }}
      />,
    );

    // Dismiss first error
    const closeBtn = screen.getByLabelText('关闭错误提示');
    await userEvent.click(closeBtn);

    // After dismiss, banner should be gone
    expect(screen.queryByRole('alert')).toBeNull();

    // Rerender with a different error - should reappear
    rerender(
      <ErrorBanner
        error={{
          code: 'ACL_DENIED',
          message: 'permission error',
        }}
      />,
    );

    expect(screen.getByRole('alert')).toBeTruthy();
    expect(screen.getByText('权限不足')).toBeTruthy();
  });
});