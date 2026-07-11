import { useEffect, useState } from "react";

import LoginView from "../features/auth/LoginView";
import SetupView from "../features/auth/SetupView";
import CommandCenterPreview from "../features/overview/CommandCenterPreview";
import ControlWorkbench from "../features/control/ControlWorkbench";
import {
  ApiError,
  getAuthStatus,
  getSystemStatus,
  getTmuxSessions,
  getManagedSessions,
  getProjects,
  getProviders,
  getSessionCsrf,
  getTeamRuns,
  type ManagedSession,
  type Project,
  type ProviderProfile,
  type AuthStatus,
  type SystemStatus,
  type TmuxSession,
  type TeamRunSummary,
} from "./api";

function readGrantFragment(): string | null {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  return params.get("grant");
}

export default function App() {
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [setupGrant] = useState(readGrantFragment);
  const [error, setError] = useState<string | null>(null);
  const [errorScope, setErrorScope] = useState<"auth" | "system">("auth");
  const [needsLogin, setNeedsLogin] = useState(false);
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [sessions, setSessions] = useState<TmuxSession[]>([]);
  const [sessionCsrf, setSessionCsrf] = useState("");
  const [providers, setProviders] = useState<ProviderProfile[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [managedSessions, setManagedSessions] = useState<ManagedSession[]>([]);
  const [teamRuns, setTeamRuns] = useState<TeamRunSummary[]>([]);

  async function loadDashboard() {
    setError(null);
    try {
      const [nextStatus, nextSessions] = await Promise.all([
        getSystemStatus(),
        getTmuxSessions(),
      ]);
      setStatus(nextStatus);
      setSessions(nextSessions);
      const [nextProviders, nextProjects, nextManaged, nextCsrf, nextTeamRuns] = await Promise.all([
        getProviders().catch(() => []),
        getProjects().catch(() => []),
        getManagedSessions().catch(() => []),
        getSessionCsrf().catch(() => ""),
        getTeamRuns().catch(() => []),
      ]);
      setProviders(nextProviders); setProjects(nextProjects); setManagedSessions(nextManaged);
      setTeamRuns(nextTeamRuns);
      if (nextCsrf) setSessionCsrf(nextCsrf);
      setNeedsLogin(false);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 401) {
        setNeedsLogin(true);
        return;
      }
      setErrorScope("system");
      setError("无法读取本机运行状态。服务仍在运行，请检查 doctor 输出后重新读取。");
    }
  }

  async function loadAuth() {
    setError(null);
    try {
      const nextAuth = await getAuthStatus();
      setAuth(nextAuth);
      if (nextAuth.configured) await loadDashboard();
    } catch {
      setErrorScope("auth");
      setError("无法读取本机服务状态。请确认 tmuxbot serve 仍在运行。");
    }
  }

  function handleAuthenticated(csrfToken: string) {
    setSessionCsrf(csrfToken);
    setAuth((current) => current ? { ...current, configured: true, setup_available: false } : current);
    void loadDashboard();
  }

  useEffect(() => {
    if (window.location.hash) {
      window.history.replaceState({}, "", `${window.location.pathname}${window.location.search}`);
    }
    void loadAuth();
  }, []);

  if (error) {
    return (
      <main className="center-message error-state" role="alert">
        <span className="fault-code">LOCAL / READ FAILURE</span>
        <strong>{error}</strong>
        <button
          className="secondary-action"
          type="button"
          onClick={() => void (errorScope === "auth" ? loadAuth() : loadDashboard())}
        >
          重新读取
        </button>
      </main>
    );
  }
  if (!auth) {
    return <main className="center-message">正在读取本机状态…</main>;
  }
  if (!auth.configured) {
    return <SetupView auth={auth} setupGrant={setupGrant} onAuthenticated={handleAuthenticated} />;
  }
  if (needsLogin) {
    return <LoginView csrfToken={auth.csrf_token} onAuthenticated={handleAuthenticated} />;
  }
  if (!status) {
    return <main className="center-message">正在验证本机登录状态…</main>;
  }
  return <>
    <CommandCenterPreview status={status} sessions={sessions} />
    <ControlWorkbench
      csrfToken={sessionCsrf}
      providers={providers}
      projects={projects}
      managedSessions={managedSessions}
      onRefresh={loadDashboard}
      teamRuns={teamRuns}
    />
  </>;
}
