import { API_MODE, isDemoMode } from './api/mode';
import { ChatPage } from './pages/ChatPage';
import { DemoChatPage } from './pages/DemoChatPage';
import { EnterpriseConsolePage } from './pages/EnterpriseConsolePage';
import { IntegrationHarnessPage } from './pages/IntegrationHarnessPage';
import { ConsoleAuthGate } from './components/layout/ConsoleAuthGate';

export function App() {
  const uiMode = (import.meta.env.VITE_UI_MODE as string | undefined)?.toLowerCase();
  const consoleMode = window.location.pathname.replace(/\/$/, '') === '/console' || uiMode === 'console';
  const page = consoleMode
    ? <EnterpriseConsolePage />
    : uiMode === 'harness'
      ? <IntegrationHarnessPage />
      : isDemoMode()
        ? <DemoChatPage />
        : <ChatPage />;

  // The deployed WebUI uses one same-origin local session for both surfaces.
  // Mock/demo modes keep their existing token-driven test behavior.
  if (API_MODE === 'gateway') {
    return <ConsoleAuthGate>{page}</ConsoleAuthGate>;
  }
  return page;
}
