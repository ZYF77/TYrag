import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MessageItem } from '../components/chat/MessageItem';
import type { ReplyMessage } from '../api/types';

describe('Error States in MessageItem', () => {
  const baseReply: Omit<ReplyMessage, 'status' | 'content' | 'error'> = {
    id: 'mock-id',
    role: 'assistant',
    citations: [],
    createdAt: '2024-01-01T00:00:00Z',
  };

  it('shows no evidence message', () => {
    const msg: ReplyMessage = {
      ...baseReply,
      status: 'no_evidence',
      content: '',
    };

    render(<MessageItem message={msg} onCitationClick={() => {}} />);

    expect(screen.getByText(/未找到可靠证据/)).toBeTruthy();
    expect(screen.getByText(/当前知识库中没有足够的证据/)).toBeTruthy();
  });

  it('shows degraded state', () => {
    const msg: ReplyMessage = {
      ...baseReply,
      status: 'degraded',
      content: '部分功能不可用的回答',
    };

    render(<MessageItem message={msg} onCitationClick={() => {}} />);

    expect(screen.getByText(/降级回答/)).toBeTruthy();
  });

  it('shows failed state with error details', () => {
    const msg: ReplyMessage = {
      ...baseReply,
      status: 'failed',
      content: '',
      error: {
        code: 'RAGFLOW_UNAVAILABLE',
        message: '知识库服务暂时不可用',
        requestId: 'req-123',
        retryable: true,
      },
    };

    render(<MessageItem message={msg} onCitationClick={() => {}} />);

    expect(screen.getByText(/RAGFLOW_UNAVAILABLE/)).toBeTruthy();
    expect(screen.getByText(/知识库服务暂时不可用/)).toBeTruthy();
    expect(screen.getByText(/req-123/)).toBeTruthy();
  });

  it('shows streaming state indicator', () => {
    const msg: ReplyMessage = {
      ...baseReply,
      status: 'streaming',
      content: '',
    };

    render(<MessageItem message={msg} onCitationClick={() => {}} />);

    expect(screen.getByText(/正在回答/)).toBeTruthy();
  });

  it('shows completed state indicator', () => {
    const msg: ReplyMessage = {
      ...baseReply,
      status: 'completed',
      content: '回答内容',
    };

    render(<MessageItem message={msg} onCitationClick={() => {}} />);

    expect(screen.getByText('回答完成')).toBeTruthy();
    expect(screen.getByText('回答内容')).toBeTruthy();
  });

  it('renders markdown-like content as text', () => {
    const msg: ReplyMessage = {
      ...baseReply,
      status: 'completed',
      content: '**设备 AX-200** 的液压系统',
    };

    render(<MessageItem message={msg} onCitationClick={() => {}} />);

    expect(screen.getByText(/AX-200/)).toBeTruthy();
  });
});
