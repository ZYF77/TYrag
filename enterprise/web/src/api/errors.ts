import { ApiError } from './client';
import type { ErrorResponse } from './types';

export function normalizeError(err: unknown): ErrorResponse {
  if (err instanceof ApiError) {
    return err.body;
  }
  if (err instanceof DOMException && err.name === 'AbortError') {
    return {
      code: 'REQUEST_CANCELLED',
      message: '请求已取消',
      requestId: 'request-cancelled',
    };
  }
  return {
    code: 'GATEWAY_UNAVAILABLE',
    message: 'Gateway 不可用或网络异常',
    requestId: 'gateway-unavailable',
  };
}
