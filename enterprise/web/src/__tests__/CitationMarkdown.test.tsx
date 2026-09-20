import { describe, it, expect } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import cases from '../../../tests/fixtures/citation-text-cases.json';
import { CitationMarkdown, rewriteCitationMarkers } from '../components/common/CitationMarkdown';
import { transformCitationMarkers } from '../components/common/citationText';
import type { Citation } from '../api/v2Types';

describe('shared citation source contract', () => {
  for (const sample of cases) {
    it(sample.name, () => {
      expect(transformCitationMarkers(sample.text, n => `[ID:${n}]`)).toBe(sample.sanitized);
      expect(rewriteCitationMarkers(sample.text, '#cite-', 'm')).toBe(sample.rendered);
      expect(rewriteCitationMarkers(sample.sanitized, '#cite-', 'm')).toBe(sample.rendered);
      expect(rewriteCitationMarkers(sample.rendered, '#cite-', 'm')).toBe(sample.rendered);
    });
  }
  it('renders body without making code or links citation buttons', () => {
    cleanup();
    render(<CitationMarkdown
      content={'[L1] arr[0] `示例[ID:2]` [手册 ID:2](https://example.invalid)\n\n```\n[ID:2]\n```\n\n正文[2]'}
      citations={[{ citationId: 'synthetic', refIndex: 2 } as Citation]}
      hrefPrefix="#cite-" markerClassName="citation" onMarkerActivate={() => {}}
    />);
    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(screen.getByRole('button', { name: '查看引用 2' })).toBeTruthy();
    expect(screen.getByRole('link', { name: '手册 ID:2' }).getAttribute('href')).toBe('https://example.invalid');
    expect(document.body.textContent).toContain('[L1] arr[0]');
    expect(document.querySelector('pre')?.textContent).toContain('[ID:2]');
  });
});
