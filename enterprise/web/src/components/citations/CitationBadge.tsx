import { FileText, Database } from 'lucide-react';
import type { Citation } from '../../api/types';

interface CitationBadgeProps {
  citation: Citation;
  index: number;
  onClick: () => void;
}

export function CitationBadge({ citation, index, onClick }: CitationBadgeProps) {
  const isDoc = citation.sourceType === 'document';

  return (
    <button
      onClick={onClick}
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md border text-xs font-medium transition-colors ${
        isDoc
          ? 'border-blue-200 bg-blue-50 text-blue-700 hover:bg-blue-100'
          : 'border-green-200 bg-green-50 text-green-700 hover:bg-green-100'
      }`}
      title={citation.title}
      aria-label={`引用 ${index}: ${citation.title}`}
    >
      {isDoc ? <FileText size={10} /> : <Database size={10} />}
      <span>[{index}]</span>
      <span className="max-w-32 truncate hidden sm:inline">{citation.title}</span>
    </button>
  );
}
