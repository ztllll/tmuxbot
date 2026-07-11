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
};

export type ProviderProfile = {
  id: string;
  binary_name: "tmux" | "claude" | "codex";
  executable_path: string;
  version?: string | null;
};

export type Project = { id: string; name: string; root_path: string };
export type ManagedSession = {
  id: string;
  project_id: string;
  provider_id: string;
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
  if (response.ok) return response.json() as Promise<T>;
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

export async function createTerminalTicket(sessionId: string, csrfToken: string) {
  return writeJson<{ ticket: string; expires_at: number }>(
    `/api/terminals/${encodeURIComponent(sessionId)}/ticket`, csrfToken,
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
