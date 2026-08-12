import { isDemoMode } from './api/mode';
import { ChatPage } from './pages/ChatPage';
import { DemoChatPage } from './pages/DemoChatPage';
import { EnterpriseConsolePage } from './pages/EnterpriseConsolePage';
import { IntegrationHarnessPage } from './pages/IntegrationHarnessPage';

export function App() {
  const uiMode = (import.meta.env.VITE_UI_MODE as string | undefined)?.toLowerCase();
  if (window.location.pathname.replace(/\/$/, '') === '/console' || uiMode === 'console') {
    return <EnterpriseConsolePage />;
  }
  if (uiMode === 'harness') return <IntegrationHarnessPage />;
  return isDemoMode() ? <DemoChatPage /> : <ChatPage />;
}
