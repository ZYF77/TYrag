import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QuestionInput } from '../components/chat/QuestionInput';

describe('QuestionInput', () => {
  it('renders input field', () => {
    render(
      <QuestionInput
        onSend={() => {}}
        onCancel={() => {}}
        isStreaming={false}
        disabled={false}
      />,
    );
    expect(screen.getByPlaceholderText(/输入您的问题/)).toBeTruthy();
  });

  it('calls onSend with trimmed value on Enter', async () => {
    let sent: string | null = null;
    render(
      <QuestionInput
        onSend={(q) => {
          sent = q;
        }}
        onCancel={() => {}}
        isStreaming={false}
        disabled={false}
      />,
    );

    const input = screen.getByPlaceholderText(/输入您的问题/);
    await userEvent.type(input, '  测试问题  ');
    await userEvent.keyboard('{Enter}');

    expect(sent).toBe('测试问题');
  });

  it('clears input after send', async () => {
    render(
      <QuestionInput
        onSend={() => {}}
        onCancel={() => {}}
        isStreaming={false}
        disabled={false}
      />,
    );

    const input = screen.getByPlaceholderText(
      /输入您的问题/,
    ) as HTMLTextAreaElement;
    await userEvent.type(input, '测试');
    await userEvent.keyboard('{Enter}');

    expect(input.value).toBe('');
  });

  it('does not send empty input', async () => {
    let sent = false;
    render(
      <QuestionInput
        onSend={() => {
          sent = true;
        }}
        onCancel={() => {}}
        isStreaming={false}
        disabled={false}
      />,
    );

    await userEvent.keyboard('{Enter}');
    expect(sent).toBe(false);
  });

  it('disables input when disabled', () => {
    render(
      <QuestionInput
        onSend={() => {}}
        onCancel={() => {}}
        isStreaming={false}
        disabled={true}
      />,
    );

    const input = screen.getByPlaceholderText(/输入您的问题/);
    expect((input as HTMLTextAreaElement).disabled).toBe(true);
  });

  it('shows stop button when streaming', () => {
    render(
      <QuestionInput
        onSend={() => {}}
        onCancel={() => {}}
        isStreaming={true}
        disabled={false}
      />,
    );

    expect(screen.getByLabelText('停止生成')).toBeTruthy();
  });

  it('calls onCancel when stop button clicked', async () => {
    let cancelled = false;
    render(
      <QuestionInput
        onSend={() => {}}
        onCancel={() => {
          cancelled = true;
        }}
        isStreaming={true}
        disabled={false}
      />,
    );

    await userEvent.click(screen.getByLabelText('停止生成'));
    expect(cancelled).toBe(true);
  });

  it('shows hint when disabled and not streaming', () => {
    render(
      <QuestionInput
        onSend={() => {}}
        onCancel={() => {}}
        isStreaming={false}
        disabled={true}
      />,
    );

    expect(screen.getByText(/请先选择一个会话/)).toBeTruthy();
  });
});
