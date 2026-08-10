import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { IntegrationHarnessPage } from '../pages/IntegrationHarnessPage';

describe('IntegrationHarnessPage', () => {
  it('replays file event, document polling, and context switch scenarios', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);

    await user.click(screen.getByRole('button', { name: '提交文件事件' }));
    await screen.findByText('未声明 ready（received）');
    expect(screen.getByText('文档状态与质量诊断')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: '创建并选择' }));
    await screen.findAllByText('Harness 会话');
    const equipment = screen.getByLabelText('equipmentId');
    await user.clear(equipment);
    await user.type(equipment, 'EQ-1002');
    await user.click(screen.getByRole('button', { name: '切换 Asset context' }));
    await waitFor(() => expect(screen.getByText(/contextVersion:/)).toBeTruthy());
  });

  it('renders independent business status and citation evidence from SSE', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    await user.click(screen.getByRole('button', { name: '创建并选择' }));
    const input = await screen.findByLabelText('问题输入');
    await user.type(input, 'how to inspect?');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText(/Harness answer/);
    await screen.findByText('业务状态：completed');
    expect(screen.getByText(/citations: 1/)).toBeTruthy();

    await user.click(screen.getByRole('button', { name: /Harness maintenance manual/ }));
    await screen.findByText('Citation snapshot');
    expect(screen.getAllByText('externalDocumentId').length).toBeGreaterThanOrEqual(1);
  });

  it('shows no-reliable-evidence independently of citation count', async () => {
    const user = userEvent.setup();
    render(<IntegrationHarnessPage />);
    await user.click(screen.getByRole('button', { name: '创建并选择' }));
    const input = await screen.findByLabelText('问题输入');
    await user.type(input, 'noevidence');
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText('业务状态：no_reliable_evidence');
    expect(screen.getByText('citations: 0')).toBeTruthy();
  });
});
