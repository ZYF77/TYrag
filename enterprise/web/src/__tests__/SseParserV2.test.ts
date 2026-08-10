import { describe, expect, it } from 'vitest';
import { createSseParser } from '../api/sse';

describe('v2 SSE parser', () => {
  it('handles split lines, CRLF, comments, multiline data, and final events', () => {
    const events: Array<{ event: string; data: string }> = [];
    const parser = createSseParser((event) => events.push(event));

    parser.feed(': heartbeat\r\nevent: answer.delta\r\ndata: {"content":"he');
    parser.feed('llo"}\r\ndata: second-line\r\n\r\nevent: run.failed\n');
    parser.feed('data: {"code":"RAGFLOW_UNAVAILABLE"}\n\n');
    parser.end();

    expect(events).toEqual([
      { event: 'answer.delta', data: '{"content":"hello"}\nsecond-line' },
      { event: 'run.failed', data: '{"code":"RAGFLOW_UNAVAILABLE"}' },
    ]);
  });

  it('turns the protocol sentinel into a stream.end event', () => {
    const events: Array<{ event: string; data: string }> = [];
    const parser = createSseParser((event) => events.push(event));
    parser.feed('data:[DONE]\n\n');
    parser.end();
    expect(events).toEqual([{ event: 'stream.end', data: '[DONE]' }]);
  });

  it('flushes an event when the stream ends without a blank line', () => {
    const events: Array<{ event: string; data: string }> = [];
    const parser = createSseParser((event) => events.push(event));
    parser.feed('event: run.failed\ndata: {"code":"SSE_FAILED"}');
    parser.end();
    expect(events).toEqual([{ event: 'run.failed', data: '{"code":"SSE_FAILED"}' }]);
  });
});
