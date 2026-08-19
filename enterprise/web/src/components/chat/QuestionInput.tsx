import { useState, useRef, useCallback, type KeyboardEvent } from 'react';
import { Send, Square } from 'lucide-react';

interface QuestionInputProps {
  onSend: (question: string) => void;
  onCancel: () => void;
  isStreaming: boolean;
  disabled: boolean;
}

export function QuestionInput({
  onSend,
  onCancel,
  isStreaming,
  disabled,
}: QuestionInputProps) {
  const [value, setValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

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

  return (
    <div className="question-composer px-4 py-3 border-t border-gray-100 bg-white">
      <div className="question-composer-field flex items-end gap-2 bg-gray-50 rounded-xl border border-gray-200 px-3 py-2 focus-within:border-blue-400 focus-within:ring-1 focus-within:ring-blue-100 transition-all">
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
      {disabled && !isStreaming && (
        <p className="text-[10px] text-gray-400 mt-1.5 text-center">
          请先选择一个会话或新建会话
        </p>
      )}
    </div>
  );
}
