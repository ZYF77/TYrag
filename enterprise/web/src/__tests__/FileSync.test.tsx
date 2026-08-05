import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { setupServer } from 'msw/node';
import { handlers } from '../api/mocks/handlers';
import { FileSyncStatus } from '../components/sync/FileSyncStatus';
import type { FileSyncItem } from '../api/types';

const server = setupServer(...handlers);

beforeEach(() => {
  server.resetHandlers();
});

const mockItems: FileSyncItem[] = [
  {
    externalDocumentId: 'ext-doc-001',
    fileName: 'AX-200维修手册v3.2.pdf',
    status: 'ready',
    stage: null,
    error: null,
    updatedAt: '2024-01-15T10:00:00Z',
  },
  {
    externalDocumentId: 'ext-doc-002',
    fileName: '设备安全规程2024.pdf',
    status: 'ready',
    stage: null,
    error: null,
    updatedAt: '2024-01-14T10:00:00Z',
  },
  {
    externalDocumentId: 'ext-doc-003',
    fileName: '液压系统图集.pdf',
    status: 'parsing',
    stage: 'ocr_processing',
    error: null,
    updatedAt: '2024-01-13T10:00:00Z',
  },
  {
    externalDocumentId: 'ext-doc-004',
    fileName: '季度保养清单.xlsx',
    status: 'failed',
    stage: null,
    error: {
      code: 'DOCUMENT_PARSE_FAILED',
      message: '文件格式不支持',
      requestId: 'req-fail-001',
      retryable: true,
    },
    updatedAt: '2024-01-12T10:00:00Z',
  },
  {
    externalDocumentId: 'ext-doc-005',
    fileName: '旧版手册.pdf',
    status: 'disabled',
    stage: null,
    error: null,
    updatedAt: '2024-01-11T10:00:00Z',
  },
];

describe('FileSyncStatus', () => {
  it('renders all sync items', () => {
    render(
      <FileSyncStatus
        items={mockItems}
        loading={false}
        onClose={() => {}}
        onRefresh={() => {}}
      />,
    );

    expect(screen.getByText('AX-200维修手册v3.2.pdf')).toBeTruthy();
    expect(screen.getByText('设备安全规程2024.pdf')).toBeTruthy();
    expect(screen.getByText('液压系统图集.pdf')).toBeTruthy();
    expect(screen.getByText('季度保养清单.xlsx')).toBeTruthy();
    expect(screen.getByText('旧版手册.pdf')).toBeTruthy();
  });

  it('shows correct status labels', () => {
    render(
      <FileSyncStatus
        items={mockItems}
        loading={false}
        onClose={() => {}}
        onRefresh={() => {}}
      />,
    );

    expect(screen.getAllByText('可查询')).toBeTruthy();
    expect(screen.getByText('解析中')).toBeTruthy();
    expect(screen.getByText('失败')).toBeTruthy();
    expect(screen.getByText('已停用')).toBeTruthy();
  });

  it('shows error messages for failed items', () => {
    render(
      <FileSyncStatus
        items={mockItems}
        loading={false}
        onClose={() => {}}
        onRefresh={() => {}}
      />,
    );

    expect(screen.getByText('文件格式不支持')).toBeTruthy();
  });

  it('shows empty state when no items', () => {
    render(
      <FileSyncStatus
        items={[]}
        loading={false}
        onClose={() => {}}
        onRefresh={() => {}}
      />,
    );

    expect(screen.getByText('暂无同步记录')).toBeTruthy();
  });

  it('shows loading state', () => {
    render(
      <FileSyncStatus
        items={[]}
        loading={true}
        onClose={() => {}}
        onRefresh={() => {}}
      />,
    );

    expect(screen.getByText('加载中...')).toBeTruthy();
  });
});
