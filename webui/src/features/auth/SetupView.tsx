import { useState, type FormEvent } from "react";

import { setupPassword, type AuthStatus } from "../../app/api";

type SetupViewProps = {
  auth: AuthStatus;
  setupGrant: string | null;
  onAuthenticated: (csrfToken: string) => void;
};

export default function SetupView({ auth, setupGrant, onAuthenticated }: SetupViewProps) {
  const grantReady = auth.setup_available && Boolean(setupGrant);
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!setupGrant || !grantReady) {
      setError("本机短期授权不可用，请从启动终端重新打开设置链接。");
      return;
    }
    if (password !== confirmation) {
      setError("两次输入的密码不一致。");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const session = await setupPassword(password, auth.csrf_token, setupGrant);
      onAuthenticated(session.csrf_token);
    } catch {
      setError("密码设置失败。授权可能已过期，请从本机终端重新打开设置链接。");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-panel" aria-labelledby="setup-title">
        <p className="eyebrow">本机控制面 · PREVIEW A</p>
        <h1 id="setup-title">设置本机访问密码</h1>
        <p className="lede">
          这一步只保护当前设备上的 tmuxbot 控制台，不会连接 Provider，也不会创建 tmux 会话。
        </p>
        <div className={`grant-state ${grantReady ? "is-ready" : "is-blocked"}`}>
          <span className="status-lamp" aria-hidden="true" />
          <div>
            <strong>{grantReady ? "本机短期授权已就绪" : "等待本机短期授权"}</strong>
            <p>
              {grantReady
                ? "授权仅用于本次设置，完成后立即失效。"
                : "请从启动 tmuxbot serve 的本机终端重新打开设置链接。"}
            </p>
          </div>
        </div>
        <form className="auth-form" onSubmit={submit}>
          <label>
            <span>新密码</span>
            <input
              type="password"
              minLength={12}
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              disabled={!grantReady || submitting}
              required
            />
          </label>
          <label>
            <span>确认密码</span>
            <input
              type="password"
              minLength={12}
              autoComplete="new-password"
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
              disabled={!grantReady || submitting}
              required
            />
          </label>
          {error && <p className="inline-error" role="alert">{error}</p>}
          <button className="primary-action" type="submit" disabled={!grantReady || submitting}>
            {submitting ? "正在设置…" : "设置密码并进入控制台"}
          </button>
        </form>
      </section>
    </main>
  );
}
