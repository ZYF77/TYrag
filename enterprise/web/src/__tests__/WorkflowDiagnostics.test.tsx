import { http, HttpResponse } from 'msw';
import { expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RagDiagnosticsPanel } from '../components/console/RagDiagnosticsPanel';
import { server } from '../test-setup';

it('shows future node and tool identities, parent executions, timings and truncation', async () => {
  server.use(
    http.get('*/admin/system/diagnostics/traces', () => HttpResponse.json({
      items: [{ runId: 'wf-fixture', status: 'completed', startedAt: '2026-09-20T00:00:00Z' }],
      hasMore: false,
    })),
    http.get('*/admin/system/diagnostics/traces/wf-fixture', () => HttpResponse.json({
      runId: 'wf-fixture', status: 'completed',
      diagnostics: { durationMs: 25, truncated: true, events: [
        { type: 'workflow_node_finished', atMs: 20, durationMs: 15, data: {
          componentId: 'new-1', componentName: '未来节点', componentType: 'FutureNode',
          spanId: 'node-execution-1', status: 'success',
        } },
        { type: 'workflow_tool_finished', atMs: 18, durationMs: 11, data: {
          componentId: 'new-1', toolName: 'future-tool', toolType: 'FutureTool',
          spanId: 'tool-execution-1', parentSpanId: 'node-execution-1', status: 'error',
        } },
      ] },
    })),
  );
  render(<RagDiagnosticsPanel />);
  const user = userEvent.setup();
  await user.click(await screen.findByText('wf-fixture'));
  const dialog = await screen.findByRole('dialog', { name: '诊断运行详情' });
  expect(await within(dialog).findByText('节点执行结束 · 未来节点')).toBeTruthy();
  expect(within(dialog).getByText('工具调用结束 · future-tool')).toBeTruthy();
  expect(within(dialog).getByText(/FutureNode · 节点 new-1/)).toBeTruthy();
  expect(within(dialog).getByText(/父执行 node-execution-1/)).toBeTruthy();
  expect(within(dialog).getByText('11ms')).toBeTruthy();
  expect(within(dialog).getByRole('status').textContent).toContain('不是完整执行记录');
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).toBeNull();
});
