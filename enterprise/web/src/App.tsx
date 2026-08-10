import { isDemoMode } from './api/mode';
import { ChatPage } from './pages/ChatPage';
import { DemoChatPage } from './pages/DemoChatPage';
import { IntegrationHarnessPage } from './pages/IntegrationHarnessPage';

export function App() {
  const uiMode = (import.meta.env.VITE_UI_MODE as string | undefined)?.toLowerCase();
  if (uiMode === 'harness') return <IntegrationHarnessPage />;
  return isDemoMode() ? <DemoChatPage /> : <ChatPage />;
}
