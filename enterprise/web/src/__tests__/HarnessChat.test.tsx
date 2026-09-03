import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { HarnessChat } from '../components/harness/HarnessChat';
import { HarnessCitationPanel } from '../components/harness/HarnessCitationPanel';
import type { Citation, ConversationDetail, HarnessAssistantMessage } from '../api/v2Types';

const conversation: ConversationDetail = {
  conversationId: 'conv-harness-ui',
  title: '测试会话',
  status: '进行中',
  equipmentId: null,
  fixedAssetNo: null,
  faultCode: null,
  contextVersion: 0,
  lastMessageAt: '2026-09-02T00:00:00Z',
  createdAt: '2026-09-02T00:00:00Z',
  context: {
    equipmentId: null,
    fixedAssetNo: null,
    faultCode: null,
    contextVersion: 0,
    registryVersion: null,
  },
};

const sourceCitation = (citationId: string, refIndex: number): Citation => ({
  citationId,
  sourceType: 'document',
  title: '同名源文档.pdf',
  externalDocumentId: 'source-document-1',
  sourceVersionId: 'v1',
  pageNo: 2,
  bbox: { x1: 0, y1: 0, x2: 1, y2: 1 },
  refIndex,
  assetId: null,
  excerpt: '解析后的 chunk',
});

function assistantMessage(citations: Citation[]): HarnessAssistantMessage {
  return {
    id: 'assistant-1',
    role: 'assistant',
    content: '结论 **已确认** [ID:1]，补充说明 [ID:2]。',
    status: '已完成',
    citations,
    createdAt: '2026-09-02T00:00:00Z',
    clientMessageId: 'client-1',
  };
}

describe('HarnessChat', () => {
  it('renders reasoning levels, internet hint, citation superscripts, and unique source chips', async () => {
    const user = userEvent.setup();
    const onCitation = vi.fn();
    const citations = [sourceCitation('citation-1', 1), sourceCitation('citation-2', 2)];
    render(
      <HarnessChat
        conversation={conversation}
        messages={[assistantMessage(citations)]}
        isStreaming={false}
        error={null}
        onSend={vi.fn()}
        onRetry={vi.fn()}
        onCancel={vi.fn()}
        onCitation={onCitation}
        reasoningMode="simple"
        onReasoningModeChange={vi.fn()}
        internetEnabled={false}
        onInternetEnabledChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('option', { name: '快速' })).toBeTruthy();
    expect(screen.getByRole('option', { name: '极致' })).toBeTruthy();
    expect(screen.queryByRole('option', { name: '0 · 快速' })).toBeNull();
    expect(screen.queryByRole('option', { name: '4 · 极致' })).toBeNull();
    expect(screen.getByTestId('reasoning-mode-hint').textContent).toContain('响应最快');
    expect(screen.getAllByRole('button', { name: '打开引用 1' })).toHaveLength(1);
    expect(screen.getAllByRole('button', { name: '打开引用 2' })).toHaveLength(1);
    expect(screen.getAllByRole('button', { name: /查看来源 同名源文档/ })).toHaveLength(1);
    expect(screen.getByText(/2 个片段/)).toBeTruthy();
    expect(document.querySelector('.harness-toggle-thumb svg')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: '打开引用 2' }));
    expect(onCitation).toHaveBeenCalledWith(citations[1]);
  });

  it('renders the reference empty state and keeps reasoning labels number-free', () => {
    render(
      <HarnessChat
        conversation={conversation}
        messages={[]}
        isStreaming={false}
        error={null}
        onSend={vi.fn()}
        onRetry={vi.fn()}
        onCancel={vi.fn()}
        onCitation={vi.fn()}
        reasoningMode="medium"
        onReasoningModeChange={vi.fn()}
        internetEnabled={false}
        onInternetEnabledChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('img', { name: '开始新的对话' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: '开始新的对话' })).toBeTruthy();
    expect(screen.getByText('输入你的问题，我将为你提供帮助')).toBeTruthy();
    expect(screen.getByRole('option', { name: '标准' })).toBeTruthy();
    expect(screen.queryByRole('option', { name: /\d/ })).toBeNull();
  });

  it('shows a thinking state before the assistant body arrives', () => {
    const message = {
      ...assistantMessage([]),
      content: '',
      status: 'streaming' as const,
      thinking: true,
    };
    render(
      <HarnessChat
        conversation={conversation}
        messages={[message]}
        isStreaming
        error={null}
        onSend={vi.fn()}
        onRetry={vi.fn()}
        onCancel={vi.fn()}
        onCitation={vi.fn()}
        reasoningMode="simple"
        onReasoningModeChange={vi.fn()}
        internetEnabled={false}
        onInternetEnabledChange={vi.fn()}
      />,
    );

    expect(screen.getByText('思考中，正在生成回答…')).toBeTruthy();
    expect(screen.getAllByText('思考中').length).toBeGreaterThan(0);
  });

  it('labels an authorized crop citation as a RAGFlow inline figure', () => {
    const citation: Citation = {
      ...sourceCitation('crop-1', 1),
      fileKind: 'crop',
      downloadUrl: '/enterprise/api/v2/citations/crop-1/file/ticket',
    };
    render(<HarnessCitationPanel citation={citation} loading={false} error={null} onClose={vi.fn()} />);

    expect(screen.getByText('RAGFlow 内嵌裁切图')).toBeTruthy();
    expect(screen.getByText('RAGFlow 内嵌裁切图（非源文件整页）')).toBeTruthy();
    expect(screen.getByRole('img', { name: /RAGFlow 内嵌裁切图/ }).getAttribute('src')).toContain('/citations/crop-1/file/ticket');
  });
});
