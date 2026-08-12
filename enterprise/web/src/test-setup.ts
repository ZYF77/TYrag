import { afterAll, afterEach, beforeAll } from 'vitest';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { handlers } from './api/mocks/handlers';

export const server = setupServer(
  http.post(
    '/enterprise/api/v1/conversations/conv-test/messages:stream',
    () => HttpResponse.error(),
  ),
  ...handlers,
);

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
