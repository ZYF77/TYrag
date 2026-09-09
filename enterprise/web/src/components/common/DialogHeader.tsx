import type { ReactNode } from 'react';

/**
 * 弹窗头部：eyebrow + 标题 + 说明行 + 关闭按钮（可附带其他操作按钮）。
 * Console 详情弹窗与 Harness 设备弹窗共用；DOM 与各调用点原写法一致。
 */
export function DialogHeader({
  eyebrow,
  title,
  titleId,
  meta,
  headClassName = 'console-detail-dialog-head',
  actions,
  actionsClassName = 'console-detail-dialog-actions',
  closeLabel,
  onClose,
  closeContent = '×',
}: {
  eyebrow: string;
  title: string;
  /** 供 aria-labelledby 引用的标题 id（如诊断详情弹窗）。 */
  titleId?: string;
  /** 标题下的说明行，如 <p className="console-route">…</p>。 */
  meta?: ReactNode;
  headClassName?: string;
  /** 关闭按钮之外的操作；提供时与关闭按钮一起包在操作容器里。 */
  actions?: ReactNode;
  actionsClassName?: string;
  closeLabel: string;
  onClose: () => void;
  /** 关闭按钮内容；Console 详情用 <X size={17} />，设备弹窗用默认“×”。 */
  closeContent?: ReactNode;
}) {
  const closeButton = (
    <button type="button" className="console-icon-button" aria-label={closeLabel} onClick={onClose}>
      {closeContent}
    </button>
  );
  return (
    <header className={headClassName}>
      <div>
        <p className="console-eyebrow">{eyebrow}</p>
        <h2 id={titleId}>{title}</h2>
        {meta}
      </div>
      {actions ? <div className={actionsClassName}>{actions}{closeButton}</div> : closeButton}
    </header>
  );
}
