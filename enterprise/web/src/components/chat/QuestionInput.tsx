import { useState, useRef, useCallback, type ChangeEvent, type KeyboardEvent } from 'react';
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
}: QuestionInputProps) {
  const [value, setValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const attachmentEnabled = Boolean(onFilesPicked && onRemoveFile);

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

  return (
    <div className="question-composer px-4 py-3 border-t border-gray-100 bg-white">
      <div className="question-composer-field flex items-end gap-2 bg-gray-50 rounded-xl border border-gray-200 px-3 py-2 focus-within:border-blue-400 focus-within:ring-1 focus-within:ring-blue-100 transition-all">
        {attachmentEnabled && (
          <>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={FILE_INPUT_ACCEPT}
              aria-label="选择附件"
              className="sr-only"
              onChange={handleFileChange}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={disabled}
              className="harness-clip-button flex-shrink-0"
              title="添加附件"
              aria-label="添加附件"
            >
              <Paperclip size={14} />
            </button>
          </>
        )}
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="输入您的问题... (Enter 发送, Shift+Enter 换行)"
          disabled={disabled}
          rows={1}
          className="flex-1 resize-none bg-transparent text-sm text-gray-700 placeholder-gray-400 outline-none max-h-32 py-1"
          aria-label="问题输入"
        />
        {isStreaming ? (
          <button
            onClick={onCancel}
            className="question-composer-action flex-shrink-0 p-1.5 rounded-lg bg-red-100 text-red-600 hover:bg-red-200 transition-colors"
            title="停止生成"
            aria-label="停止生成"
          >
            <Square size={14} />
          </button>
        ) : (
          <button
            onClick={handleSend}
            disabled={!value.trim() || disabled}
            className="question-composer-action flex-shrink-0 p-1.5 rounded-lg bg-blue-600 text-white hover:bg-blue-700 disabled:bg-gray-200 disabled:text-gray-400 transition-colors"
            title="发送"
            aria-label="发送"
          >
            <Send size={14} />
          </button>
        )}
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
        <p className="text-[10px] text-gray-400 mt-1.5 text-center">
          请先选择一个会话或新建会话
        </p>
      )}
    </div>
  );
}
