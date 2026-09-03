import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type RefObject,
} from 'react';
import { createPortal } from 'react-dom';

export type ConsoleOverlayMode = 'popover' | 'dialog';

interface ConsoleOverlayProps {
  open: boolean;
  mode?: ConsoleOverlayMode;
  onClose: () => void;
  children: ReactNode;
  anchorRef?: RefObject<HTMLElement | null>;
  ariaLabel?: string;
  labelledBy?: string;
  className?: string;
  initialFocusRef?: RefObject<HTMLElement | null>;
}

interface OverlayPosition {
  top: number;
  left: number;
}

function firstFocusable(node: HTMLElement): HTMLElement | null {
  return node.querySelector<HTMLElement>(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
  );
}

/**
 * Small dependency-free overlay primitive shared by Console and Harness.
 * Popovers stay attached to their trigger; dialogs use a light dismiss layer.
 */
export function ConsoleOverlay({
  open,
  mode = 'dialog',
  onClose,
  children,
  anchorRef,
  ariaLabel,
  labelledBy,
  className,
  initialFocusRef,
}: ConsoleOverlayProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [position, setPosition] = useState<OverlayPosition | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  const updatePosition = useCallback(() => {
    if (mode !== 'popover' || !anchorRef?.current || !panelRef.current) return;
    const anchor = anchorRef.current.getBoundingClientRect();
    const panel = panelRef.current.getBoundingClientRect();
    const margin = 16;
    let left = anchor.left;
    let top = anchor.bottom + 8;
    if (left + panel.width > window.innerWidth - margin) {
      left = window.innerWidth - margin - panel.width;
    }
    if (left < margin) left = margin;
    if (top + panel.height > window.innerHeight - margin) {
      top = anchor.top - panel.height - 8;
    }
    if (top < margin) top = margin;
    setPosition({ top, left });
  }, [anchorRef, mode]);

  useLayoutEffect(() => {
    if (!open) {
      setPosition(null);
      return undefined;
    }
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const frame = window.requestAnimationFrame(() => {
      updatePosition();
      const target = initialFocusRef?.current ?? firstFocusable(panelRef.current!);
      target?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [initialFocusRef, open, updatePosition]);

  useEffect(() => {
    if (!open) return undefined;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCloseRef.current();
      }
    };
    const handlePointerDown = (event: PointerEvent) => {
      if (event.button !== 0) return;
      const target = event.target as Node;
      if (panelRef.current?.contains(target)) return;
      if (mode === 'popover' && anchorRef?.current?.contains(target)) return;
      onCloseRef.current();
    };
    const handleViewportChange = () => updatePosition();
    document.addEventListener('keydown', handleKeyDown);
    document.addEventListener('pointerdown', handlePointerDown);
    window.addEventListener('resize', handleViewportChange);
    window.addEventListener('scroll', handleViewportChange, true);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      document.removeEventListener('pointerdown', handlePointerDown);
      window.removeEventListener('resize', handleViewportChange);
      window.removeEventListener('scroll', handleViewportChange, true);
      previousFocusRef.current?.focus();
    };
  }, [anchorRef, mode, open, updatePosition]);

  if (!open) return null;

  const style = mode === 'popover'
    ? ({
      ...(position ? { top: position.top, left: position.left } : {}),
      visibility: position ? 'visible' : 'hidden',
    } satisfies CSSProperties)
    : undefined;

  return createPortal(
    (
    <div
      className={`console-overlay-layer console-overlay-layer--${mode}`}
      onPointerDown={mode === 'dialog' ? (event) => event.stopPropagation() : undefined}
    >
      {mode === 'dialog' && (
        <button
          type="button"
          className="console-overlay-scrim"
          aria-label="关闭弹窗（点击遮罩）"
          onClick={onClose}
        />
      )}
      <div
        ref={panelRef}
        className={`console-overlay-panel${className ? ` ${className}` : ''}`}
        style={style}
        role="dialog"
        aria-modal={mode === 'dialog' ? true : undefined}
        aria-label={ariaLabel}
        aria-labelledby={labelledBy}
      >
        {children}
      </div>
    </div>
    ),
    document.body,
  );
}
