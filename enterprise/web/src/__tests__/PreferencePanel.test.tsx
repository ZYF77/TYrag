import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PreferencePanel } from '../components/harness/PreferencePanel';
import { preferenceApi } from '../api/v2Client';
vi.mock('../api/v2Client', () => ({ preferenceApi: { list: vi.fn(), decide: vi.fn(), remove: vi.fn() } }));
const candidate = { id: 'c1', key: 'detail', value: 'brief', revision: 0 };
beforeEach(() => { vi.resetAllMocks(); });
describe('confirmed preferences', () => {
  it('does not confirm until the user acts and reloads after confirmation', async () => {
    vi.mocked(preferenceApi.list).mockResolvedValue({ enabled: true, candidates: [candidate], preferences: [] });
    render(<PreferencePanel conversationId="c" revision="1" busy={false} />);
    await screen.findByText('以后记住“简短回答”？');
    expect(preferenceApi.decide).not.toHaveBeenCalled();
    vi.mocked(preferenceApi.list).mockResolvedValue({ enabled: true, candidates: [], preferences: [{ ...candidate, revision: 1 }] });
    await userEvent.click(screen.getByRole('button', { name: '确认' }));
    expect(preferenceApi.decide).toHaveBeenCalledWith(candidate, true);
    await screen.findByRole('button', { name: '删除偏好' });
    await userEvent.click(screen.getByRole('button', { name: '删除偏好' }));
    expect(preferenceApi.remove).toHaveBeenCalledWith(expect.objectContaining({ revision: 1 }));
  });
  it('waits for terminal state and keeps failed confirmation visible', async () => {
    vi.mocked(preferenceApi.list).mockResolvedValue({ enabled: true, candidates: [candidate], preferences: [] });
    const { rerender } = render(<PreferencePanel conversationId="c" revision="1" busy />);
    expect(preferenceApi.list).not.toHaveBeenCalled();
    rerender(<PreferencePanel conversationId="c" revision="2" busy={false} />);
    await screen.findByRole('button', { name: '忽略' });
    vi.mocked(preferenceApi.decide).mockRejectedValue(new Error('synthetic conflict'));
    await userEvent.click(screen.getByRole('button', { name: '确认' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('偏好未更新'));
    expect(screen.getByRole('button', { name: '忽略' })).toBeTruthy();
  });
});
