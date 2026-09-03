import { ApiError } from './client';
import type {
  DemoAskRequest,
  DemoAskResponse,
  DemoConversation,
  DemoDocumentStatus,
  ErrorResponse,
  UserPrincipal,
} from './types';

const BASE = '/enterprise/api/v1';
const TOKEN_STORAGE_KEY = 'enterprise.demo.jwt';

export function getDemoToken(): string {
  const stored = sessionStorage.getItem(TOKEN_STORAGE_KEY);
  if (stored) {
    return stored;
  }
  return (import.meta.env.VITE_DEMO_JWT as string | undefined) ?? '';
}

export function setDemoToken(token: string): void {
  const trimmed = token.trim();
  if (trimmed) {
    sessionStorage.setItem(TOKEN_STORAGE_KEY, trimmed);
  } else {
    sessionStorage.removeItem(TOKEN_STORAGE_KEY);
  }
}

function authHeaders(): Record<string, string> {
  const token = getDemoToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = (await res.json().catch(() => ({
      code: `HTTP_${res.status}`,
      message: `Gateway returned HTTP ${res.status}`,
      requestId: 'unknown',
    }))) as ErrorResponse;
    throw new ApiError(res.status, body);
  }
  return res.json();
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  try {
    const res = await fetch(`${BASE}${path}`, {
      ...init,
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders(),
        ...(init.headers ?? {}),
      },
    });
    return handleResponse<T>(res);
  } catch (err) {
    if (err instanceof ApiError) {
      throw err;
    }
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw err;
    }
    throw new ApiError(0, {
      code: 'GATEWAY_UNAVAILABLE',
      message: 'Gateway 不可用或网络异常',
      requestId: 'gateway-unavailable',
    });
  }
}

export const demoApi = {
  async getMe(): Promise<UserPrincipal> {
    return request<UserPrincipal>('/auth/me');
  },

  async getDocumentStatus(
    externalDocumentId: string,
  ): Promise<DemoDocumentStatus> {
    return request<DemoDocumentStatus>(
      `/demo/documents/${encodeURIComponent(externalDocumentId)}/status`,
    );
  },

  async ask(req: DemoAskRequest): Promise<DemoAskResponse> {
    return request<DemoAskResponse>('/demo/ask', {
      method: 'POST',
      body: JSON.stringify(req),
    });
  },

  async getConversation(
    conversationId: string,
  ): Promise<DemoConversation> {
    return request<DemoConversation>(
      `/demo/conversations/${encodeURIComponent(conversationId)}`,
    );
  },
};
