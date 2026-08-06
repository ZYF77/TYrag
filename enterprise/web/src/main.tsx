import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import { isMockMode } from './api/mode';
import './index.css';

async function bootstrap() {
  // MSW only runs in mock mode; demo/gateway use the real backend.
  if (import.meta.env.DEV && isMockMode()) {
    const { worker } = await import('./api/mocks/browser');
    await worker.start({ onUnhandledRequest: 'bypass' });
  }

  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}

bootstrap();
