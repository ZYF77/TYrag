import { ChatPage } from './pages/ChatPage';
import { DemoChatPage } from './pages/DemoChatPage';
import { isDemoMode } from './api/mode';

export function App() {
  return isDemoMode() ? <DemoChatPage /> : <ChatPage />;
}
