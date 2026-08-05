import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { setupServer } from 'msw/node';
import { handlers } from '../api/mocks/handlers';
import { ConversationItem } from '../components/layout/ConversationItem';
import { Sidebar } from '../components/layout/Sidebar';
import type { Conversation } from '../api/types';

const server = setupServer(...handlers);

beforeEach(() => {
  server.resetHandlers();
});

describe('ConversationItem', () => {
  const mockConv: Conversation = {
    conversationId: 'conv-test',
    ragflowSessionId: 'sess-test',
    createdAt: '2024-01-15T10:00:00Z',
    title: '测试会话',
    equipmentId: 'EQ-1001',
    fixedAssetNo: null,
    faultCode: null,
  };

  it('renders conversation title and date', () => {
    render(
      <ConversationItem
        conversation={mockConv}
        isActive={false}
        onClick={() => {}}
      />,
    );
    expect(screen.getByText('测试会话')).toBeTruthy();
    expect(screen.getByText(/EQ-1001/)).toBeTruthy();
  });

  it('shows active state styling', () => {
    render(
      <ConversationItem
        conversation={mockConv}
        isActive={true}
        onClick={() => {}}
      />,
    );
    const btn = screen.getByRole('button');
    expect(btn.className).toContain('bg-blue-50');
  });

  it('handles click events', async () => {
    let clicked = false;
    render(
      <ConversationItem
        conversation={mockConv}
        isActive={false}
        onClick={() => {
          clicked = true;
        }}
      />,
    );
    await userEvent.click(screen.getByRole('button'));
    expect(clicked).toBe(true);
  });
});

describe('Sidebar', () => {
  const mockConversations: Conversation[] = [
    {
      conversationId: 'conv-1',
      ragflowSessionId: 'sess-1',
      createdAt: '2024-01-15T10:00:00Z',
      title: '会话一',
    },
    {
      conversationId: 'conv-2',
      ragflowSessionId: 'sess-2',
      createdAt: '2024-01-14T10:00:00Z',
      title: '会话二',
    },
  ];

  it('renders new conversation button', () => {
    render(
      <Sidebar
        conversations={mockConversations}
        activeId={null}
        loading={false}
        onSelect={() => {}}
        onNew={() => {}}
        onRefresh={() => {}}
        syncCount={0}
        onToggleSyncDrawer={() => {}}
        syncDrawerOpen={false}
      />,
    );
    expect(screen.getByText('新建会话')).toBeTruthy();
  });

  it('renders all conversations', () => {
    render(
      <Sidebar
        conversations={mockConversations}
        activeId={null}
        loading={false}
        onSelect={() => {}}
        onNew={() => {}}
        onRefresh={() => {}}
        syncCount={0}
        onToggleSyncDrawer={() => {}}
        syncDrawerOpen={false}
      />,
    );
    expect(screen.getByText('会话一')).toBeTruthy();
    expect(screen.getByText('会话二')).toBeTruthy();
  });

  it('shows empty state when no conversations', () => {
    render(
      <Sidebar
        conversations={[]}
        activeId={null}
        loading={false}
        onSelect={() => {}}
        onNew={() => {}}
        onRefresh={() => {}}
        syncCount={0}
        onToggleSyncDrawer={() => {}}
        syncDrawerOpen={false}
      />,
    );
    expect(screen.getByText('暂无会话记录')).toBeTruthy();
  });

  it('calls onNew when button clicked', async () => {
    let called = false;
    render(
      <Sidebar
        conversations={mockConversations}
        activeId={null}
        loading={false}
        onSelect={() => {}}
        onNew={() => {
          called = true;
        }}
        onRefresh={() => {}}
        syncCount={0}
        onToggleSyncDrawer={() => {}}
        syncDrawerOpen={false}
      />,
    );
    await userEvent.click(screen.getByText('新建会话'));
    expect(called).toBe(true);
  });
});
