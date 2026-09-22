import { useEffect, useState } from 'react';
import { preferenceApi } from '../../api/v2Client';

export interface Preference { key: string; value: string; revision: number }
export interface Candidate extends Preference { id: string }
export interface PreferenceState { enabled: boolean; preferences: Preference[]; candidates: Candidate[] }
const labels: Record<string, string> = {
  zh: '简体中文', en: '英语', brief: '简短回答', detailed: '详细回答',
  paragraphs: '使用段落', list: '使用列表',
};

export function PreferencePanel({ conversationId, revision, busy }: {
  conversationId: string; revision: string; busy: boolean;
}) {
  const [state, setState] = useState<PreferenceState | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    let active = true;
    setState(null);
    if (!busy) {
      preferenceApi.list(conversationId).then(value => { if (active) setState(value); })
        .catch(() => { if (active) setState(null); });
    }
    return () => { active = false; };
  }, [conversationId, revision, busy]);
  async function change(action: () => Promise<unknown>) {
    setSaving(true); setError('');
    try { await action(); setState(await preferenceApi.list(conversationId)); }
    catch { setError('偏好未更新，请刷新后重试。'); }
    finally { setSaving(false); }
  }
  if (!state || (!state.preferences.length && !state.candidates.length)) return null;
  return <section aria-label="表达偏好" className="harness-preferences">
    <p>表达偏好{!state.enabled ? '（已暂停使用）' : ''}。修改偏好可直接说“以后请详细回答”等，确认后生效。</p>
    {state.candidates.map(item => <div key={item.id}>
      <span>以后记住“{labels[item.value]}”？</span>
      <button type="button" className="console-secondary-button" disabled={saving} onClick={() => void change(() => preferenceApi.decide(item, true))}>确认</button>
      <button type="button" className="console-secondary-button" disabled={saving} onClick={() => void change(() => preferenceApi.decide(item, false))}>忽略</button>
    </div>)}
    {state.preferences.map(item => <div key={item.key}>
      <span>{labels[item.value]}</span>
      <button type="button" className="console-secondary-button" disabled={saving} onClick={() => void change(() => preferenceApi.remove(item))}>删除偏好</button>
    </div>)}
    {error && <p role="alert">{error}</p>}
  </section>;
}
