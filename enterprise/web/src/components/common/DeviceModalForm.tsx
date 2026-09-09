import type { ReactNode } from 'react';
import { DialogHeader } from './DialogHeader';

/**
 * Harness 设备弹窗表单骨架（「换绑设备」与「指定设备创建」共用）：
 * eyebrow + 标题 + 关闭按钮、diag-field 设备编号输入、取消 + 提交按钮。
 * 提交守卫与提交动作由调用方决定；error 插槽原样渲染在帮助文案之后。
 */
export function DeviceModalForm({
  title,
  description,
  closeLabel,
  onClose,
  equipmentAriaLabel,
  equipmentPlaceholder,
  equipmentValue,
  onEquipmentChange,
  helpText,
  error,
  submitLabel,
  submittingLabel,
  submitting = false,
  submitDisabled = false,
  onCancel,
  onSubmit,
}: {
  title: string;
  description: string;
  closeLabel: string;
  onClose: () => void;
  equipmentAriaLabel: string;
  equipmentPlaceholder: string;
  equipmentValue: string;
  onEquipmentChange: (value: string) => void;
  helpText: string;
  /** 弹窗内错误展示（ConsoleAlert 或 ErrorBanner），由调用方按现状传入。 */
  error?: ReactNode;
  submitLabel: string;
  submittingLabel: string;
  submitting?: boolean;
  submitDisabled?: boolean;
  onCancel: () => void;
  onSubmit: () => void;
}) {
  return (
    <form
      className="harness-device-modal-form"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <DialogHeader
        headClassName="harness-device-modal-head"
        eyebrow="可选上下文"
        title={title}
        meta={<p>{description}</p>}
        closeLabel={closeLabel}
        onClose={onClose}
      />
      <div className="harness-device-modal-body">
        <label className="diag-field">
          设备编号 <small>equipmentId</small>
          <input aria-label={equipmentAriaLabel} value={equipmentValue} onChange={(event) => onEquipmentChange(event.target.value)} placeholder={equipmentPlaceholder} className="diag-input" autoFocus />
        </label>
        <p className="diag-help">{helpText}</p>
        {error}
      </div>
      <div className="harness-device-modal-actions">
        <button type="button" className="console-secondary-button" onClick={onCancel}>取消</button>
        <button type="submit" disabled={submitting || submitDisabled} className="console-primary-button">
          {submitting ? submittingLabel : submitLabel}
        </button>
      </div>
    </form>
  );
}
