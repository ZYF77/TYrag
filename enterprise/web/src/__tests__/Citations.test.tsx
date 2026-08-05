import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { setupServer } from 'msw/node';
import { handlers } from '../api/mocks/handlers';
import { CitationBadge } from '../components/citations/CitationBadge';
import { CitationDrawer } from '../components/citations/CitationDrawer';
import type { Citation } from '../api/types';

const server = setupServer(...handlers);

beforeEach(() => {
  server.resetHandlers();
});

const docCitation: Citation = {
  citationId: 'cit-001',
  sourceType: 'document',
  title: 'AX-200 维修手册 v3.2',
  documentId: 'doc-001',
  versionId: 'ver-001',
  pageNo: 37,
  bbox: { x1: 0.1, y1: 0.2, x2: 0.8, y2: 0.4 },
  assetId: null,
  excerpt: '当设备出现 E-104 错误代码时...',
  recordType: null,
  recordId: null,
};

const bizCitation: Citation = {
  citationId: 'cit-003',
  sourceType: 'business_record',
  title: '最近维修记录',
  documentId: null,
  versionId: null,
  pageNo: null,
  bbox: null,
  assetId: 'EQ-1001',
  excerpt: '2024-11-15 更换液压泵密封件',
  recordType: 'maintenance',
  recordId: 'WO-2024-0892',
};

describe('CitationBadge', () => {
  it('renders document citation with index', () => {
    render(
      <CitationBadge citation={docCitation} index={1} onClick={() => {}} />,
    );
    expect(screen.getByText('[1]')).toBeTruthy();
    expect(screen.getByText(/AX-200/)).toBeTruthy();
  });

  it('renders business record citation', () => {
    render(
      <CitationBadge citation={bizCitation} index={2} onClick={() => {}} />,
    );
    expect(screen.getByText('[2]')).toBeTruthy();
    expect(screen.getByText(/最近维修记录/)).toBeTruthy();
  });

  it('calls onClick when clicked', async () => {
    let clicked = false;
    render(
      <CitationBadge
        citation={docCitation}
        index={1}
        onClick={() => {
          clicked = true;
        }}
      />,
    );
    await userEvent.click(screen.getByRole('button'));
    expect(clicked).toBe(true);
  });

  it('has different styling for document vs business', () => {
    const { container: docContainer } = render(
      <CitationBadge citation={docCitation} index={1} onClick={() => {}} />,
    );
    const { container: bizContainer } = render(
      <CitationBadge citation={bizCitation} index={2} onClick={() => {}} />,
    );

    const docBtn = docContainer.querySelector('button')!;
    const bizBtn = bizContainer.querySelector('button')!;

    expect(docBtn.className).toContain('border-blue');
    expect(bizBtn.className).toContain('border-green');
  });
});

describe('CitationDrawer', () => {
  it('shows empty prompt when no citation selected', () => {
    render(<CitationDrawer citationId={null} onClose={() => {}} />);
    expect(screen.getByText(/点击回答中的引用编号/)).toBeTruthy();
  });

  it('loads and displays citation details', async () => {
    render(
      <CitationDrawer citationId="cit-001" onClose={() => {}} />,
    );

    await waitFor(() => {
      expect(screen.getByText('AX-200 维修手册 v3.2')).toBeTruthy();
    });

    expect(screen.getByText(/页码: 37/)).toBeTruthy();
    expect(screen.getByText(/文档: doc-001/)).toBeTruthy();
  });

  it('shows business record citation details', async () => {
    render(
      <CitationDrawer citationId="cit-003" onClose={() => {}} />,
    );

    // Wait for loading to complete (loading text disappears)
    await waitFor(() => {
      expect(screen.queryByText('加载中...')).toBeNull();
    }, { timeout: 3000 });

    // After loading, check the drawer shows content (not empty state)
    expect(screen.queryByText(/点击回答中的引用编号/)).toBeNull();

    // Verify business record source type badge is visible
    const container = document.body.textContent ?? '';
    expect(container).toContain('业务记录');
    expect(container).toContain('WO-2024-0892');
  });

  it('handles citation not found / 403', async () => {
    render(
      <CitationDrawer citationId="nonexistent" onClose={() => {}} />,
    );

    await waitFor(() => {
      expect(screen.getByText(/不存在或无权访问/)).toBeTruthy();
    });
  });

  it('calls onClose when close button clicked', async () => {
    let closed = false;
    render(
      <CitationDrawer
        citationId={null}
        onClose={() => {
          closed = true;
        }}
      />,
    );

    await userEvent.click(screen.getByLabelText('关闭引用抽屉'));
    expect(closed).toBe(true);
  });
});
