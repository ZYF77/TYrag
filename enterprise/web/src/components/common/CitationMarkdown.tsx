import ReactMarkdown from 'react-markdown';
import type { Citation } from '../../api/v2Types';

import { transformCitationMarkers } from './citationText';
export { CITATION_MARKER_PATTERN } from './citationText';

export function citationForMarker(citations: Citation[], marker: number): Citation | undefined {
  return citations.find((citation) => citation.refIndex === marker) ?? citations[marker - 1];
}

/** 把正文中的引用角标改写成指向 citation 锚点的 markdown 链接。 */
export function rewriteCitationMarkers(content: string, hrefPrefix: string, messageId?: string): string {
  return transformCitationMarkers(content, (marker) => {
    const suffix = messageId ? `${messageId}-${marker}` : `${marker}`;
    return `[${marker}](${hrefPrefix}${suffix})`;
  });
}

/**
 * 共享的引用 markdown 渲染：
 * - Console 侧（会话详情 / 会话管理）角标为 `<a>` 上标，可访问名“查看引用 N”；
 * - Harness 侧角标为 `<button>` 上标（传 onMarkerActivate），可访问名“打开引用 N”。
 * 角标无对应 citation 时：console-inspector 保持普通链接（missingMarker="link"），
 * 会话管理与 Harness 渲染纯数字上标（missingMarker="marker"）。
 */
export function CitationMarkdown({
  content,
  citations,
  hrefPrefix,
  messageId,
  markerClassName,
  onMarkerActivate,
  markerAriaLabel = '查看引用',
  missingMarker = 'marker',
  className,
}: {
  content: string;
  citations: Citation[];
  hrefPrefix: string;
  messageId?: string;
  markerClassName: string;
  onMarkerActivate?: (citation: Citation, marker: number) => void;
  markerAriaLabel?: string;
  missingMarker?: 'marker' | 'link';
  className?: string;
}) {
  const markerPrefix = messageId ? `${hrefPrefix}${messageId}-` : hrefPrefix;
  const body = (
    <ReactMarkdown
      components={{
        a: ({ href, children, ...props }) => {
          const marker = href?.startsWith(markerPrefix)
            ? Number(href.slice(markerPrefix.length))
            : Number.NaN;
          if (Number.isInteger(marker) && marker > 0) {
            const citation = citationForMarker(citations, marker);
            if (citation) {
              return onMarkerActivate ? (
                <sup className={markerClassName}>
                  <button
                    type="button"
                    onClick={() => onMarkerActivate(citation, marker)}
                    aria-label={`${markerAriaLabel} ${marker}`}
                  >
                    {marker}
                  </button>
                </sup>
              ) : (
                <sup className={markerClassName}>
                  <a href={href} {...props} aria-label={`${markerAriaLabel} ${marker}`}>{marker}</a>
                </sup>
              );
            }
            if (missingMarker === 'marker') {
              return <sup className={markerClassName}>{marker}</sup>;
            }
          }
          return <a href={href} {...props} target="_blank" rel="noreferrer">{children}</a>;
        },
      }}
    >
      {rewriteCitationMarkers(content, hrefPrefix, messageId)}
    </ReactMarkdown>
  );
  return className ? <div className={className}>{body}</div> : body;
}
