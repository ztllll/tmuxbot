import { useState, type FormEvent } from "react";

import { loginPassword } from "../../app/api";

type LoginViewProps = {
  csrfToken: string;
  onAuthenticated: (csrfToken: string) => void;
};

export default function LoginView({ csrfToken, onAuthenticated }: LoginViewProps) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const session = await loginPassword(password, csrfToken);
      onAuthenticated(session.csrf_token);
    } catch {
      setError("密码不正确，控制台未解锁。");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-panel compact" aria-labelledby="login-title">
        <p className="eyebrow">LOCAL ACCESS · 127.0.0.1</p>
        <h1 id="login-title">登录本机控制台</h1>
        <p className="lede">使用首次设置的密码查看本机 tmux、Provider 和 bridge 状态。</p>
        <form className="auth-form" onSubmit={submit}>
          <label>
            <span>访问密码</span>
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              disabled={submitting}
              required
              autoFocus
            />
          </label>
          {error && <p className="inline-error" role="alert">{error}</p>}
          <button className="primary-action" type="submit" disabled={submitting}>
            {submitting ? "正在验证…" : "登录控制台"}
          </button>
        </form>
      </section>
    </main>
  );
}
