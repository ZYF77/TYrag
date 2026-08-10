import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ErrorBanner } from '../components/errors/ErrorBanner';

describe('v2 error diagnostics', () => {
  it.each([
    [401, 'AUTH_TOKEN_INVALID'],
    [403, 'ACL_DENIED'],
    [409, 'CONVERSATION_CONTEXT_STALE'],
    [422, 'VALIDATION_ERROR'],
    [503, 'RAGFLOW_UNAVAILABLE'],
  ])('shows HTTP %s and the stable code', (httpStatus, code) => {
    render(
      <ErrorBanner
        error={{
          code,
          message: 'diagnostic message',
          requestId: `request-${httpStatus}`,
          retryable: httpStatus === 503,
          httpStatus,
        }}
      />,
    );
    expect(screen.getByRole('alert')).toBeTruthy();
    expect(screen.getByText(`HTTP ${httpStatus}${httpStatus === 503 ? ' · retryable' : ''}`)).toBeTruthy();
    expect(screen.getByText('diagnostic message')).toBeTruthy();
  });
});
