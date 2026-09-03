import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { UserRound } from 'lucide-react';
import { setHarnessToken, toDisplayError, v2Api } from '../../api/v2Client';
import type { ConsoleAuthSession } from '../../api/consoleTypes';
import './console-auth.css';

interface ConsoleAuthGateProps {
  children: ReactNode;
}

type AuthState = 'checking' | 'signed-out' | 'signed-in' | 'error';

export function ConsoleAuthGate({ children }: ConsoleAuthGateProps) {
  const [state, setState] = useState<AuthState>('checking');
  const [session, setSession] = useState<ConsoleAuthSession | null>(null);
  const [error, setError] = useState<string | null>(null);
  const sessionMenuRef = useRef<HTMLDetailsElement | null>(null);

  const checkSession = useCallback(async () => {
    setState('checking');
    setError(null);
    try {
      const current = await v2Api.getConsoleAuth();
      setSession(current);
      setState('signed-in');
    } catch (reason) {
      const display = toDisplayError(reason);
      if (display.httpStatus === 401) {
        setSession(null);
        setError(null);
        setState('signed-out');
      } else {
        setError(display.message);
        setState('error');
      }
    }
  }, []);

  useEffect(() => {
    // Gateway mode no longer uses the legacy sessionStorage Bearer input.
    setHarnessToken('');
    void checkSession();
  }, [checkSession]);

  useEffect(() => {
    const onExpired = () => {
      setSession(null);
      setState('signed-out');
      setError('本地会话已过期，请重新登录。');
    };
    window.addEventListener('enterprise-console-auth-expired', onExpired);
    return () => window.removeEventListener('enterprise-console-auth-expired', onExpired);
  }, []);

  useEffect(() => {
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (event.button !== 0) return;
      const menu = sessionMenuRef.current;
      if (menu?.open && !menu.contains(event.target as Node)) menu.open = false;
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      const menu = sessionMenuRef.current;
      if (event.key !== 'Escape' || !menu?.open) return;
      event.preventDefault();
      menu.open = false;
      menu.querySelector<HTMLElement>('summary')?.focus();
    };
    document.addEventListener('pointerdown', closeOnOutsidePointer);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsidePointer);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, []);

  const logout = useCallback(async () => {
    try {
      await v2Api.logoutConsole();
    } finally {
      setSession(null);
      setState('signed-out');
      setError(null);
    }
  }, []);

  if (state === 'checking') {
    return <div className="console-auth-page" data-testid="console-auth-loading"><div className="console-auth-card"><p>正在检查本地会话…</p></div></div>;
  }

  if (state === 'error') {
    return (
      <div className="console-auth-page" data-testid="console-auth-error">
        <div className="console-auth-card">
          <p className="console-auth-kicker">Gateway WebUI</p>
          <h1>无法连接登录服务</h1>
          <p className="console-auth-message">{error ?? '请稍后重试。'}</p>
          <button type="button" className="console-auth-primary" onClick={() => void checkSession()}>重试</button>
        </div>
      </div>
    );
  }

  if (state === 'signed-out') {
    return <ConsoleLoginForm initialError={error} onSuccess={(current) => { setSession(current); setError(null); setState('signed-in'); }} />;
  }

  return (
    <>
      <details ref={sessionMenuRef} className="console-auth-session" data-testid="console-auth-session">
        <summary className="console-auth-session-trigger" aria-label="打开运维账号菜单">
          <UserRound size={17} aria-hidden="true" />
          <span className="console-auth-session-label">运维账号</span>
        </summary>
        <div className="console-auth-session-menu">
          <div className="console-auth-session-heading">
            <strong>运维账号</strong>
            <span>{session?.username ?? 'zkadmin'}</span>
          </div>
          <span className="console-auth-session-tenant">租户 · {session?.tenantId ?? '已配置'}</span>
          <button type="button" onClick={() => void logout}>退出登录</button>
        </div>
      </details>
      {children}
    </>
  );
}

function ConsoleLoginForm({
  initialError,
  onSuccess,
}: {
  initialError: string | null;
  onSuccess: (session: ConsoleAuthSession) => void;
}) {
  const [username, setUsername] = useState('zkadmin');
  const [password, setPassword] = useState('');
  const [error, setError] = useState(initialError);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const current = await v2Api.loginConsole(username, password);
      setPassword('');
      onSuccess(current);
    } catch (reason) {
      setPassword('');
      setError(toDisplayError(reason).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="console-auth-page" data-testid="console-auth-login">
      <form className="console-auth-card" onSubmit={submit}>
        <p className="console-auth-kicker">Gateway WebUI</p>
        <h1>运维登录</h1>
        <p className="console-auth-message">Console 与 Harness 共用一次登录会话。</p>
        <label htmlFor="console-auth-username">账号</label>
        <input
          id="console-auth-username"
          name="username"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          autoComplete="username"
          required
        />
        <label htmlFor="console-auth-password">密码</label>
        <input
          id="console-auth-password"
          name="password"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoComplete="current-password"
          required
        />
        {error && <p role="alert" className="console-auth-error">{error}</p>}
        <button type="submit" className="console-auth-primary" disabled={submitting}>
          {submitting ? '登录中…' : '登录'}
        </button>
      </form>
    </div>
  );
}
