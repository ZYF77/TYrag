import { act, renderHook, waitFor, render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { useV2Chat } from '../hooks/useV2Chat';
import { v2Api } from '../api/v2Client';
import { QuestionInput } from '../components/chat/QuestionInput';

vi.mock('../api/v2Client', () => ({
  v2Api: { listMessages: vi.fn(), streamMessage: vi.fn() },
  validateMessageFiles: vi.fn(),
  toDisplayError: (e: unknown) => e,
}));

describe('run ownership rejection', () => {
  it.each(['CONVERSATION_BUSY', 'CONVERSATION_RESTART_REQUIRED'])('retains draft and removes unsaved messages for %s', async (code) => {
    vi.mocked(v2Api.listMessages).mockResolvedValue({ items: [], hasMore: false, nextCursor: null });
    vi.mocked(v2Api.streamMessage).mockImplementation(() => ({ controller: new AbortController(), promise: Promise.reject({ code, message: code, requestId: 'synthetic' }) }));
    const { result } = renderHook(() => useV2Chat('synthetic'));
    await act(async () => {});
    const file = new File(['synthetic'], 'synthetic.txt');
    act(() => { result.current.sendMessage('preserve this question', [file]); });
    await waitFor(() => expect(result.current.error?.code).toBe(code));
    expect(result.current.messages).toEqual([]);
    expect(result.current.rejectedDraft).toEqual({ question: 'preserve this question', files: [file] });
  });

  it('restores rejected text into the composer', () => {
    render(<QuestionInput onSend={vi.fn()} onCancel={vi.fn()} isStreaming={false} disabled={false} restoredDraft={{ question: 'retained' }} />);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('retained');
  });
});
