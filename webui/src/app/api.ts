export type AuthStatus = {
  configured: boolean;
  setup_available: boolean;
  setup_expires_at?: number | null;
  csrf_token: string;
};

export type SystemStatus = {
  host?: string | {
    hostname?: string;
    platform?: string;
    python_version?: string;
  };
  bridge?: string | {
    status?: string;
    reason?: string;
  };
  tmux?: string | {
    status?: string;
    version?: string;
  };
  paths?: Record<string, string | { path?: string; status?: string }>;
  providers?: Array<{
    name?: string;
    status?: string;
    version?: string;
    path?: string;
  }>;
};

export type TmuxSession = {
  target: string;
  session_name: string;
  command: string;
  cwd: string;
  classification: string;
  provider?: string | null;
  window_index?: number;
  pane_index?: number;
};

export type ProviderProfile = {
  id: string;
  binary_name: string;
  executable_path: string;
  version?: string | null;
  capabilities?: ProviderCapabilities;
};
export type ProviderCapabilities = {
  display_name: string;
  managed: boolean;
  supports_model_picker: boolean;
  model_command?: string | null;
};

export type Project = { id: string; name: string; root_path: string };
export type ManagedSession = {
  id: string;
  project_id: string;
  provider_id: string;
  provider?: string | null;
  provider_capabilities?: ProviderCapabilities | null;
  runtime_model?: string | null;
  name: string;
  tmux_target: string;
  status: string;
};

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function readJson<T>(response: Response): Promise<T> {
  if (response.ok) {
    if (response.status === 204) return undefined as T;
    return response.json() as Promise<T>;
  }
  let message = "请求失败";
  try {
    const payload = (await response.json()) as { detail?: string };
    if (payload.detail) message = payload.detail;
  } catch {
    // Keep the stable local error message when the server response is not JSON.
  }
  throw new ApiError(message, response.status);
}

export async function getAuthStatus(): Promise<AuthStatus> {
  return readJson<AuthStatus>(
    await fetch("/api/auth/status", { credentials: "same-origin" }),
  );
}

export async function setupPassword(
  password: string,
  csrfToken: string,
  setupGrant: string,
): Promise<{ csrf_token: string }> {
  return readJson(
    await fetch("/api/auth/setup", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
        "X-Setup-Token": setupGrant,
      },
      body: JSON.stringify({ password }),
    }),
  );
}

export async function loginPassword(
  password: string,
  csrfToken: string,
): Promise<{ csrf_token: string }> {
  return readJson(
    await fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify({ password }),
    }),
  );
}

export async function getSystemStatus(): Promise<SystemStatus> {
  return readJson<SystemStatus>(
    await fetch("/api/system/status", { credentials: "same-origin" }),
  );
}

export async function getSessionCsrf(): Promise<string> {
  const result = await readJson<{ csrf_token: string }>(
    await fetch("/api/auth/session", { credentials: "same-origin" }),
  );
  return result.csrf_token;
}

export async function getTmuxSessions(): Promise<TmuxSession[]> {
  return readJson<TmuxSession[]>(
    await fetch("/api/tmux/sessions", { credentials: "same-origin" }),
  );
}

async function writeJson<T>(path: string, csrfToken: string, body?: unknown): Promise<T> {
  return readJson<T>(await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: body === undefined ? undefined : JSON.stringify(body),
  }));
}

export async function getProviders(): Promise<ProviderProfile[]> {
  return readJson(await fetch("/api/providers", { credentials: "same-origin" }));
}

export async function scanProviders(csrfToken: string): Promise<ProviderProfile[]> {
  return writeJson("/api/providers/scan", csrfToken);
}

export async function probeProvider(id: string, csrfToken: string) {
  return writeJson<{ success: boolean; version?: string | null; error_code?: string | null }>(
    `/api/providers/${encodeURIComponent(id)}/probe`, csrfToken,
  );
}

export async function getProjects(): Promise<Project[]> {
  return readJson(await fetch("/api/projects", { credentials: "same-origin" }));
}

export async function createProject(name: string, rootPath: string, csrfToken: string): Promise<Project> {
  return writeJson("/api/projects", csrfToken, { name, root_path: rootPath });
}

export async function inspectProject(rootPath: string, csrfToken: string) {
  return writeJson<{ root_path: string; git_root?: string | null; branch?: string | null; matching_panes: Array<{ target: string; command: string }> }>(
    "/api/projects/inspect", csrfToken, { root_path: rootPath },
  );
}

export async function updateProject(id: string, name: string, rootPath: string, csrfToken: string): Promise<Project> {
  return readJson(await fetch(`/api/projects/${encodeURIComponent(id)}`, {
    method: "PATCH", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify({ name, root_path: rootPath }),
  }));
}

export async function deleteProject(id: string, csrfToken: string): Promise<void> {
  await readJson<void>(await fetch(`/api/projects/${encodeURIComponent(id)}`, {
    method: "DELETE", credentials: "same-origin", headers: { "X-CSRF-Token": csrfToken },
  }));
}

export async function getManagedSessions(): Promise<ManagedSession[]> {
  return readJson(await fetch("/api/managed-sessions", { credentials: "same-origin" }));
}

export async function createManagedSession(
  name: string, projectId: string, providerId: string, csrfToken: string,
): Promise<ManagedSession> {
  return writeJson("/api/managed-sessions", csrfToken, {
    name, project_id: projectId, provider_id: providerId,
  });
}

export async function releaseManagedSession(id: string, csrfToken: string): Promise<void> {
  await readJson<void>(await fetch(`/api/managed-sessions/${encodeURIComponent(id)}`, {
    method: "DELETE", credentials: "same-origin", headers: { "X-CSRF-Token": csrfToken },
  }));
}

export async function adoptManagedSession(
  input: { name: string; projectId: string; providerId: string; target: string }, csrfToken: string,
): Promise<ManagedSession> {
  return writeJson("/api/managed-sessions/adopt", csrfToken, {
    name: input.name, project_id: input.projectId, provider_id: input.providerId, target: input.target,
  });
}

export async function createTerminalTicket(sessionId: string, csrfToken: string) {
  return writeJson<{ ticket: string; expires_at: number }>(
    `/api/terminals/${encodeURIComponent(sessionId)}/ticket`, csrfToken,
  );
}

export async function createObservedTerminalTicket(target: string, csrfToken: string) {
  return writeJson<{ terminal_id: string; ticket: string; expires_at: number }>(
    "/api/terminals/observed/ticket", csrfToken, { target },
  );
}

export async function startTerminalTakeover(sessionId: string, csrfToken: string) {
  return writeJson<{ mode: string }>(
    `/api/terminals/${encodeURIComponent(sessionId)}/takeover`, csrfToken,
  );
}

export async function releaseTerminalTakeover(sessionId: string, csrfToken: string) {
  return readJson<{ mode: string }>(await fetch(
    `/api/terminals/${encodeURIComponent(sessionId)}/takeover`,
    { method: "DELETE", credentials: "same-origin", headers: { "X-CSRF-Token": csrfToken } },
  ));
}

export async function configureChannel(
  body: {
    channel: "telegram" | "feishu";
    managed_session_id: string;
    remote_chat_id: string;
    credential_id: string;
    credential_secret?: string;
    boss_id: string;
    mention_required: boolean;
  },
  csrfToken: string,
) {
  return writeJson<{ channel: string; configured: boolean; restart_required: boolean }>(
    "/api/channels/configure", csrfToken, body,
  );
}

export type TeamRunSnapshot = {
  run: { run_id: string; goal: string; state: string };
  agents: Array<{ agent_id: string; role: string; managed_session_id: string }>;
  tasks: Array<{ task_id: string; title: string; goal: string; role: string; state: string; dependencies: string[]; requires_write: boolean; attempt: number; assignee_agent_id?: string | null }>;
};
export type TeamTaskInput = {
  taskId: string;
  title: string;
  goal: string;
  role: "coordinator" | "implementer";
  dependencies: string[];
  requiresWrite: boolean;
};

export type TeamRunSummary = {
  run_id: string;
  goal: string;
  state: string;
};
export type TeamRunEvent = {
  sequence: number; event_id: string; event_type: string; aggregate_type: string;
  aggregate_id: string; payload: Record<string, unknown>; occurred_at: string;
};
export type DispatchReceipt = {
  command_id: string; task_id: string; attempt: number; managed_session_id: string;
  state: "pending" | "tmux_written" | "uncertain"; created_at: string;
  tmux_written_at: string | null; last_error: string | null;
};
export type TaskWorktree = {
  task_id: string; attempt: number; path: string; branch: string;
  managed_session_id: string; state: "active" | "released";
};

export async function createTeamRun(
  input: { runId: string; goal: string; coordinator: string; implementer: string; reviewer: string; tasks: TeamTaskInput[] },
  csrfToken: string,
): Promise<TeamRunSnapshot> {
  return writeJson("/api/team-runs", csrfToken, {
    run_id: input.runId, goal: input.goal, idempotency_key: `create-${input.runId}`,
    agents: [
      { role: "coordinator", managed_session_id: input.coordinator },
      { role: "implementer", managed_session_id: input.implementer },
      { role: "reviewer", managed_session_id: input.reviewer },
    ],
    tasks: input.tasks.map((task) => ({ task_id: task.taskId, title: task.title, goal: task.goal, role: task.role, dependencies: task.dependencies, requires_write: task.requiresWrite, max_attempts: 2 })),
  });
}

export async function launchTeamRun(
  input: {
    projectName: string; rootPath: string; runId: string; goal: string;
    roles: Array<{ role: "coordinator" | "implementer" | "reviewer"; providerId: string; name: string }>;
  },
  csrfToken: string,
): Promise<TeamRunSnapshot> {
  return writeJson("/api/team-runs/launch", csrfToken, {
    project_name: input.projectName,
    root_path: input.rootPath,
    run_id: input.runId,
    goal: input.goal,
    idempotency_key: `launch-${input.runId}`,
    roles: input.roles.map((role) => ({
      role: role.role, provider_id: role.providerId, name: role.name,
    })),
  });
}

export async function commandTeamRun(runId: string, command: "start" | "pause" | "resume", csrfToken: string) {
  return writeJson<TeamRunSnapshot>(`/api/team-runs/${encodeURIComponent(runId)}/${command}`, csrfToken, { idempotency_key: `${command}-${Date.now()}` });
}

export async function completeTeamTask(runId: string, taskId: string, agentId: string, artifactUri: string, csrfToken: string) {
  return writeJson<{ state: string }>(`/api/team-runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/complete`, csrfToken, {
    agent_id: agentId, idempotency_key: `complete-${taskId}-${Date.now()}`,
    artifacts: [{ kind: "implementation_evidence", uri: artifactUri, metadata: { source: "operator-confirmed" } }],
  });
}

export async function reviewTeamTask(runId: string, taskId: string, verdict: "approved" | "rejected", notes: string, csrfToken: string) {
  return writeJson<{ state: string }>(`/api/team-runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(taskId)}/review`, csrfToken, {
    reviewer_agent_id: `${runId}:reviewer`, verdict, notes, idempotency_key: `review-${taskId}-${Date.now()}`,
  });
}

export async function getTeamRun(runId: string): Promise<TeamRunSnapshot> {
  return readJson(await fetch(`/api/team-runs/${encodeURIComponent(runId)}`, { credentials: "same-origin" }));
}

export async function getTeamRuns(): Promise<TeamRunSummary[]> {
  return readJson(await fetch("/api/team-runs", { credentials: "same-origin" }));
}

export async function getTeamRunEvents(runId: string): Promise<TeamRunEvent[]> {
  return readJson(await fetch(`/api/team-runs/${encodeURIComponent(runId)}/events`, { credentials: "same-origin" }));
}

export async function getDispatchReceipts(runId: string): Promise<DispatchReceipt[]> {
  return readJson(await fetch(`/api/team-runs/${encodeURIComponent(runId)}/dispatches`, { credentials: "same-origin" }));
}

export async function getTaskWorktrees(runId: string): Promise<TaskWorktree[]> {
  return readJson(await fetch(`/api/team-runs/${encodeURIComponent(runId)}/worktrees`, { credentials: "same-origin" }));
}

export async function mergeTaskWorktree(runId: string, taskId: string, attempt: number, csrfToken: string) {
  return writeJson<{ merged: boolean; branch: string }>(
    `/api/team-runs/${encodeURIComponent(runId)}/worktrees/${encodeURIComponent(taskId)}/${attempt}/merge`,
    csrfToken,
    { idempotency_key: `merge-${taskId}-${attempt}-${Date.now()}` },
  );
}

export async function releaseTaskWorktree(runId: string, taskId: string, attempt: number, csrfToken: string) {
  return readJson<{ released: boolean }>(await fetch(
    `/api/team-runs/${encodeURIComponent(runId)}/worktrees/${encodeURIComponent(taskId)}/${attempt}`,
    { method: "DELETE", credentials: "same-origin", headers: { "X-CSRF-Token": csrfToken } },
  ));
}
