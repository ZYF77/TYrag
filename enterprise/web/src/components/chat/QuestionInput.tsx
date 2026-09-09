import { useState, useRef, useCallback, useEffect, type ChangeEvent, type KeyboardEvent, type ReactNode } from 'react';
import { Paperclip, Send, Square, X } from 'lucide-react';

interface QuestionInputProps {
  onSend: (question: string) => void;
  onCancel: () => void;
  isStreaming: boolean;
  disabled: boolean;
  /** Optional attachment support (used by the Harness page). When omitted the
   * paperclip and file chips are not rendered at all, so the main chat is
   * unaffected. */
  onFilesPicked?: (files: File[]) => void;
  selectedFiles?: File[];
  onRemoveFile?: (index: number) => void;
  /** Optional Harness-only toolbar controls rendered inside the expanded composer. */
  variant?: 'default' | 'harness';
  composerTools?: ReactNode;
  /** Clear draft text when the surrounding conversation changes. */
  resetKey?: string | number;
}

const FILE_INPUT_ACCEPT = '.jpg,.jpeg,.png,.txt,.pdf,.docx,.xlsx';

export function QuestionInput({
  onSend,
  onCancel,
  isStreaming,
  disabled,
  onFilesPicked,
  selectedFiles,
  onRemoveFile,
  variant = 'default',
  composerTools,
  resetKey,
}: QuestionInputProps) {
  const [value, setValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const attachmentEnabled = Boolean(onFilesPicked && onRemoveFile);
  const harnessVariant = variant === 'harness';

  useEffect(() => {
    if (resetKey !== undefined) setValue('');
  }, [resetKey]);

  const handleSend = useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue('');
    textareaRef.current?.focus();
  }, [value, disabled, onSend]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  const handleFileChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const picked = Array.from(event.target.files ?? []);
      if (picked.length > 0) onFilesPicked?.(picked);
      event.target.value = '';
    },
    [onFilesPicked],
  );

  const attachmentControls = attachmentEnabled ? (
    <>
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={FILE_INPUT_ACCEPT}
        aria-label="选择附件"
        className="question-composer-sr-only"
        onChange={handleFileChange}
      />
      <button
        type="button"
        onClick={() => fileInputRef.current?.click()}
        disabled={disabled}
        className="harness-clip-button question-composer-clip"
        title="添加附件"
        aria-label="添加附件"
      >
        <Paperclip size={harnessVariant ? 18 : 14} />
      </button>
    </>
  ) : null;

  const sendControl = isStreaming ? (
    <button
      onClick={onCancel}
      className="question-composer-action question-composer-action--stop"
      title="停止生成"
      aria-label="停止生成"
    >
      <Square size={harnessVariant ? 16 : 14} />
    </button>
  ) : (
    <button
      onClick={handleSend}
      disabled={!value.trim() || disabled}
      className="question-composer-action question-composer-action--send"
      title="发送"
      aria-label="发送"
    >
      <Send size={harnessVariant ? 18 : 14} />
    </button>
  );

  return (
    <div className={`question-composer ${harnessVariant ? 'question-composer--harness' : 'question-composer--default'}`}>
      <div className={`question-composer-field ${harnessVariant ? 'question-composer-field--harness' : 'question-composer-field--default'}`}>
        {!harnessVariant && attachmentControls}
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="输入您的问题... (Enter 发送, Shift+Enter 换行)"
          disabled={disabled}
          rows={harnessVariant ? 4 : 1}
          className={harnessVariant ? 'question-composer-textarea--harness' : 'question-composer-textarea--default'}
          aria-label="问题输入"
        />
        {harnessVariant ? (
          <div className="question-composer-toolbar">
            <div className="question-composer-toolbar-start">
              {attachmentControls}
              {composerTools}
            </div>
            {sendControl}
          </div>
        ) : sendControl}
      </div>
      {attachmentEnabled && selectedFiles && selectedFiles.length > 0 && (
        <div className="harness-file-chips">
          {selectedFiles.map((file, index) => (
            <span key={`${file.name}-${index}`} className="harness-file-chip">
              <span className="harness-file-chip-name">{file.name}</span>
              <button
                type="button"
                onClick={() => onRemoveFile?.(index)}
                aria-label={`移除附件 ${file.name}`}
                className="harness-file-chip-remove"
              >
                <X size={10} />
              </button>
            </span>
          ))}
        </div>
      )}
      {disabled && !isStreaming && (
        <p className="question-composer-hint">
          请先选择一个会话或新建会话
        </p>
      )}
    </div>
  );
}
