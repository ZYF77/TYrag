import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { ConsoleOverlay } from '../components/console/ConsoleOverlay';

function OverlayFixture() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>打开弹窗</button>
      <ConsoleOverlay open={open} mode="dialog" onClose={() => setOpen(false)} ariaLabel="测试弹窗">
        <div>
          <h2>测试弹窗</h2>
          <button type="button" onClick={() => setOpen(false)}>关闭</button>
        </div>
      </ConsoleOverlay>
    </>
  );
}

describe('ConsoleOverlay', () => {
  it('closes on Escape and restores focus to the trigger', async () => {
    const user = userEvent.setup();
    render(<OverlayFixture />);
    const trigger = screen.getByRole('button', { name: '打开弹窗' });
    await user.click(trigger);
    expect(screen.getByRole('dialog', { name: '测试弹窗' })).toBeTruthy();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog', { name: '测试弹窗' })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it('closes when the light scrim is clicked', async () => {
    const user = userEvent.setup();
    render(<OverlayFixture />);
    await user.click(screen.getByRole('button', { name: '打开弹窗' }));
    await user.click(screen.getByRole('button', { name: '关闭弹窗（点击遮罩）' }));
    expect(screen.queryByRole('dialog', { name: '测试弹窗' })).toBeNull();
  });
});
