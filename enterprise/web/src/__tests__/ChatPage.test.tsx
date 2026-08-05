import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, waitFor, } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { setupServer } from 'msw/node';
import { handlers } from '../api/mocks/handlers';
import { ChatPage } from '../pages/ChatPage';

const server = setupServer(...handlers);

beforeEach(() => {
  server.resetHandlers();
});

describe('ChatPage Integration', () => {
  it('renders the full layout with sidebar and main', async () => {
    render(<ChatPage />);

    await waitFor(() => {
      expect(screen.getByText('企业知识库')).toBeTruthy();
      expect(screen.getByText('新建会话')).toBeTruthy();
      expect(screen.getByText('历史会话')).toBeTruthy();
    });
  });

  it('loads and displays conversation list', async () => {
    render(<ChatPage />);

    await waitFor(() => {
      expect(screen.getByText('AX-200 报修流程咨询')).toBeTruthy();
      expect(screen.getByText('设备保养周期查询')).toBeTruthy();
      expect(screen.getByText('液压系统故障排查')).toBeTruthy();
    });
  });

  it('creates a new conversation when button clicked', async () => {
    render(<ChatPage />);

    const newBtn = screen.getByText('新建会话');
    await userEvent.click(newBtn);

    await waitFor(() => {
      expect(screen.getByText('新会话')).toBeTruthy();
    });
  });

  it('opens file sync drawer when sync icon clicked', async () => {
    render(<ChatPage />);

    const syncBtn = screen.getByLabelText('文件同步状态');
    await userEvent.click(syncBtn);

    await waitFor(() => {
      expect(screen.getByText('文件同步状态')).toBeTruthy();
      expect(screen.getByText('AX-200维修手册v3.2.pdf')).toBeTruthy();
    });
  });

  it('can close file sync drawer', async () => {
    render(<ChatPage />);

    const syncBtn = screen.getByLabelText('文件同步状态');
    await userEvent.click(syncBtn);

    await waitFor(() => {
      expect(screen.getByText('文件同步状态')).toBeTruthy();
    });

    const closeBtn = screen.getByLabelText('关闭文件同步面板');
    await userEvent.click(closeBtn);

    await waitFor(() => {
      expect(screen.queryByText('文件同步状态')).toBeNull();
    });
  });

  it('types into question input and can clear it', async () => {
    render(<ChatPage />);

    await waitFor(() => {
      expect(screen.getByText('AX-200 报修流程咨询')).toBeTruthy();
    });

    // Click on existing conversation to enable input
    const convBtn = screen.getByText('AX-200 报修流程咨询');
    await userEvent.click(convBtn);

    await waitFor(() => {
      expect(screen.getByText('EQ-1001')).toBeTruthy();
    });

    // The input should now be enabled
    const input = screen.getByPlaceholderText(/输入您的问题/) as HTMLTextAreaElement;
    expect(input.disabled).toBe(false);

    // Type something
    await userEvent.type(input, '测试问题');
    expect(input.value).toBe('测试问题');

    // Clear input
    await userEvent.clear(input);
    expect(input.value).toBe('');
  });

  it('shows hidden menu restrictions (no advanced menus for regular users)', async () => {
    render(<ChatPage />);

    await waitFor(() => {
      expect(screen.getByText('企业知识库')).toBeTruthy();
    });

    // These admin-only items should NOT be visible
    expect(screen.queryByText('Agent Canvas')).toBeNull();
    expect(screen.queryByText('知识库管理')).toBeNull();
    expect(screen.queryByText('系统设置')).toBeNull();
    expect(screen.queryByText('用户管理')).toBeNull();
  });
});
