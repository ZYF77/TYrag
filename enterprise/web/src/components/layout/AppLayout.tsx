import type { ReactNode } from 'react';

interface AppLayoutProps {
  sidebar: ReactNode;
  main: ReactNode;
  drawer?: ReactNode;
}

export function AppLayout({ sidebar, main, drawer }: AppLayoutProps) {
  return (
    <div className="flex h-screen w-screen overflow-hidden bg-white">
      {/* Sidebar */}
      <aside className="w-72 flex-shrink-0 border-r border-gray-200 bg-gray-50 flex flex-col">
        {sidebar}
      </aside>

      {/* Main */}
      <main className="flex-1 flex flex-col min-w-0 overflow-hidden">
        {main}
      </main>

      {/* Drawer */}
      {drawer && (
        <aside className="w-80 flex-shrink-0 border-l border-gray-200 bg-white">
          {drawer}
        </aside>
      )}
    </div>
  );
}
