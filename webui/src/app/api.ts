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

export async function getTmuxSessions(): Promise<TmuxSession[]> {
  return readJson<TmuxSession[]>(
    await fetch("/api/tmux/sessions", { credentials: "same-origin" }),
  );
}
