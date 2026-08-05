import type { ReactNode } from 'react';

interface SidebarSectionProps {
  title: string;
  children: ReactNode;
}

export function SidebarSection({ title, children }: SidebarSectionProps) {
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="px-3 pt-3 pb-1">
        <p className="text-[10px] font-medium uppercase tracking-wider text-gray-400">
          {title}
        </p>
      </div>
      <div className="px-1.5">{children}</div>
    </div>
  );
}
