import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { setDemoToken } from '../api/demoClient';
import { DemoChatPage } from '../pages/DemoChatPage';

const DOC_ID_KEY = 'enterprise.demo.externalDocumentId';
const CONVERSATIONS_KEY = 'enterprise.demo.conversations';

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  setDemoToken('test-jwt');
});

describe('DemoChatPage', () => {
  it('asks a ready document and displays answer with citations', async () => {
    localStorage.setItem(DOC_ID_KEY, 'E2E-Doc1');
    render(<DemoChatPage />);

    await waitFor(() => {
      expect(screen.getByText('可查询')).toBeTruthy();
    });

    await userEvent.click(screen.getByText('新建会话'));

    const input = screen.getByPlaceholderText(/输入您的问题/);
    expect((input as HTMLTextAreaElement).disabled).toBe(false);
    await userEvent.type(input, '故障码 E-104 怎么处理？');
    expect((input as HTMLTextAreaElement).value).toBe('故障码 E-104 怎么处理？');
    const sendButton = screen.getByLabelText('发送') as HTMLButtonElement;
    expect(sendButton.disabled).toBe(false);
    await userEvent.click(sendButton);

    await waitFor(() => {
      expect(screen.getByText(/answer for: 故障码 E-104/)).toBeTruthy();
      expect(screen.getByText('Doc1.pdf')).toBeTruthy();
    });
  });

  it('keeps no reliable evidence as a business status', async () => {
    localStorage.setItem(DOC_ID_KEY, 'E2E-Doc1');
    render(<DemoChatPage />);

    await waitFor(() => {
      expect(screen.getByText('可查询')).toBeTruthy();
    });

    await userEvent.click(screen.getByText('新建会话'));

    const input = screen.getByPlaceholderText(/输入您的问题/);
    await userEvent.type(input, 'noevidence query');
    const sendButton = screen.getByLabelText('发送') as HTMLButtonElement;
    await userEvent.click(sendButton);

    await waitFor(() => {
      expect(screen.getByText('未找到可靠证据')).toBeTruthy();
      expect(screen.getByText('未找到可靠依据，无法回答。')).toBeTruthy();
    });
  });

  it('restores persisted conversation history on load', async () => {
    localStorage.setItem(DOC_ID_KEY, 'E2E-Doc1');
    localStorage.setItem(
      CONVERSATIONS_KEY,
      JSON.stringify([
        {
          conversationId: 'demo-conv-existing',
          externalDocumentId: 'E2E-Doc1',
          createdAt: new Date().toISOString(),
          title: '历史会话',
          persisted: true,
        },
      ]),
    );

    render(<DemoChatPage />);

    await waitFor(() => {
      expect(screen.getAllByText('历史问题').length).toBeGreaterThanOrEqual(2);
      expect(screen.getByText('历史回答')).toBeTruthy();
    });
  });

  it('disables asking while document is parsing', async () => {
    localStorage.setItem(DOC_ID_KEY, 'E2E-PARSING');
    render(<DemoChatPage />);

    await waitFor(() => {
      expect(screen.getByText('解析中')).toBeTruthy();
    });

    await userEvent.click(screen.getByText('新建会话'));
    const input = screen.getByPlaceholderText(/输入您的问题/) as HTMLTextAreaElement;
    expect(input.disabled).toBe(true);
  });

  it('shows 403 when document is not allowed', async () => {
    localStorage.setItem(DOC_ID_KEY, 'E2E-FORBIDDEN');
    render(<DemoChatPage />);

    await waitFor(() => {
      expect(screen.getByText('权限不足')).toBeTruthy();
    });
  });
});
