import { ConsoleOverlay } from '../console/ConsoleOverlay';

export interface AdvancedSearchField<K extends string = string> {
  key: K;
  label: string;
  placeholder: string;
  type?: 'search' | 'number';
}

/**
 * 组合条件高级检索弹窗：Console 会话元数据与文件元数据共用一套骨架，
 * 字段配置（*_ADVANCED_FIELDS）由调用方传入。
 * 会话侧输入带 type/min/step（typedInput），文件侧输入无 type 属性。
 */
export function AdvancedSearchOverlay<K extends string>({
  open,
  onClose,
  ariaLabel,
  sectionId,
  testId,
  sectionLabel,
  fields,
  draft,
  onFieldChange,
  onClearDraft,
  onCancel,
  onApply,
  typedInput = false,
  idPrefix,
}: {
  open: boolean;
  onClose: () => void;
  ariaLabel: string;
  sectionId: string;
  testId: string;
  sectionLabel: string;
  fields: ReadonlyArray<AdvancedSearchField<K>>;
  draft: Record<K, string>;
  onFieldChange: (key: K, value: string) => void;
  onClearDraft: () => void;
  onCancel: () => void;
  onApply: () => void;
  typedInput?: boolean;
  idPrefix: string;
}) {
  return (
    <ConsoleOverlay
      open={open}
      mode="dialog"
      onClose={onClose}
      ariaLabel={ariaLabel}
      className="console-advanced-search-overlay"
    >
      <section id={sectionId} className="console-advanced-search" data-testid={testId} aria-label={sectionLabel}>
        <div className="console-advanced-search-head">
          <div>
            <strong>组合条件</strong>
            <span>多个条件同时满足；未填写的条件不会参与筛选</span>
          </div>
          <button type="button" className="console-text-button" onClick={onClearDraft}>清空条件</button>
        </div>
        <div className="console-advanced-search-grid">
          {fields.map((field) => (
            <label key={field.key} htmlFor={`${idPrefix}-${field.key}`}>
              <span>{field.label}</span>
              <input
                id={`${idPrefix}-${field.key}`}
                type={typedInput ? (field.type ?? 'search') : undefined}
                min={typedInput && field.type === 'number' ? 0 : undefined}
                step={typedInput && field.type === 'number' ? 1 : undefined}
                value={draft[field.key]}
                placeholder={field.placeholder}
                onChange={(event) => onFieldChange(field.key, event.target.value)}
              />
            </label>
          ))}
        </div>
        <div className="console-advanced-search-actions">
          <button type="button" className="console-secondary-button" onClick={onCancel}>取消</button>
          <button type="button" className="console-primary-button" onClick={onApply}>应用条件</button>
        </div>
      </section>
    </ConsoleOverlay>
  );
}
