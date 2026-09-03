import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { ConsoleOverlay } from '../console/ConsoleOverlay';
import type { ConversationDetail, DisplayError, PatchConversationContextRequest } from '../../api/v2Types';

interface HarnessContextBarProps {
  conversation: ConversationDetail | null;
  saving: boolean;
  error: DisplayError | null;
  onSave: (context: PatchConversationContextRequest) => void;
}

function formatConversationTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '未提供';
  const pad = (part: number) => String(part).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * Compact session header above the chat column: current title + device badge
 * plus an inline "换绑设备" form backed by PATCH /conversations/{id}/context.
 * Replaces the old CONTEXT card / ContextEditor (no registry snapshot,
 * no registryVersion/contextVersion/faultCode display).
 */
export function HarnessContextBar({ conversation, saving, error, onSave }: HarnessContextBarProps) {
  const [open, setOpen] = useState(false);
  const [equipmentId, setEquipmentId] = useState('');
  const [copyStatus, setCopyStatus] = useState<'idle' | 'copied' | 'failed'>('idle');
  const copyStatusRef = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    setEquipmentId(conversation?.equipmentId ?? '');
    setOpen(false);
    setCopyStatus('idle');
  }, [conversation]);

  const canSubmit = Boolean(equipmentId.trim());

  const copyConversationId = useCallback(async () => {
    if (!conversation?.conversationId) return;
    let success = false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(conversation.conversationId);
        success = true;
      }
    } catch {
      // HTTP/LAN pages commonly reject navigator.clipboard; use the native fallback below.
    }
    if (!success) {
      const textarea = document.createElement('textarea');
      textarea.value = conversation.conversationId;
      textarea.setAttribute('readonly', '');
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      try {
        success = document.execCommand('copy');
      } catch {
        success = false;
      } finally {
        textarea.remove();
      }
    }
    setCopyStatus(success ? 'copied' : 'failed');
    if (copyStatusRef.current) copyStatusRef.current.textContent = success ? '已复制' : '复制失败';
    window.setTimeout(() => {
      setCopyStatus('idle');
      if (copyStatusRef.current) copyStatusRef.current.textContent = '';
    }, 1600);
  }, [conversation?.conversationId]);

  return (
    <div className="harness-context-bar">
      {!conversation && <span className="console-empty">未选择会话。</span>}
      {conversation && (
        <>
          <div className="harness-context-top">
            <div className="harness-context-summary">
              <strong>{conversation.title}</strong>
              <span data-testid="harness-device-badge" className="harness-device-badge">
                设备: {conversation.equipmentId ?? '未绑定设备'}
              </span>
            </div>
            <button
              type="button"
              onClick={() => setOpen((previous) => !previous)}
              aria-expanded={open}
              className="console-secondary-button harness-rebind-button"
            >
              换绑设备
            </button>
          </div>
          <div className="harness-context-meta" aria-label="会话信息">
            <div className="harness-context-meta-item harness-context-meta-item--id">
              <span>会话 ID</span>
              <code>{conversation.conversationId}</code>
              <button
                type="button"
                className="harness-copy-button"
                aria-label="复制会话 ID"
                title="复制会话 ID"
                onClick={() => void copyConversationId()}
              >
                {copyStatus === 'copied' ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
              </button>
              {copyStatus !== 'idle' && <span className={`harness-copy-status${copyStatus === 'failed' ? ' is-failed' : ''}`} aria-hidden="true">{copyStatus === 'copied' ? '已复制' : '复制失败'}</span>}
              <span ref={copyStatusRef} className="sr-only" aria-live="polite" />
            </div>
            <div className="harness-context-meta-item">
              <span>来源</span>
              <strong>Gateway 会话</strong>
            </div>
            <div className="harness-context-meta-item">
              <span>创建时间</span>
              <time dateTime={conversation.createdAt}>{formatConversationTime(conversation.createdAt)}</time>
            </div>
          </div>
        </>
      )}
      <ConsoleOverlay
        open={Boolean(conversation && open)}
        mode="dialog"
        onClose={() => setOpen(false)}
        ariaLabel="换绑设备"
        className="harness-device-modal harness-rebind-modal"
      >
        <form
          className="harness-device-modal-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!canSubmit) return;
            onSave({ equipmentId: equipmentId.trim() });
          }}
        >
          <div className="harness-device-modal-head">
            <div>
              <p className="console-eyebrow">可选上下文</p>
              <h2>换绑设备</h2>
              <p>后续检索会优先限定在新设备资料内。</p>
            </div>
            <button type="button" className="console-icon-button" aria-label="关闭换绑设备" onClick={() => setOpen(false)}>×</button>
          </div>
          <div className="harness-device-modal-body">
            <label className="diag-field">
              设备编号 <small>equipmentId</small>
              <input aria-label="equipmentId" value={equipmentId} onChange={(event) => setEquipmentId(event.target.value)} placeholder="新的设备编号" className="diag-input" autoFocus />
            </label>
            <p className="diag-help">只提交设备编号；清空后不会覆盖当前绑定。</p>
            {error && <p className="console-alert">[{error.code}] {error.message}</p>}
          </div>
          <div className="harness-device-modal-actions">
            <button type="button" className="console-secondary-button" onClick={() => setOpen(false)}>取消</button>
            <button type="submit" disabled={saving || !canSubmit} className="console-primary-button">
              {saving ? '保存中…' : '保存换绑'}
            </button>
          </div>
        </form>
      </ConsoleOverlay>
    </div>
  );
}
