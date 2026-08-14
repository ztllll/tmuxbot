import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { createHash } from "node:crypto";
import { mkdir, realpath, rename, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";

type HealthState = "idle" | "working" | "recovering" | "terminal_error";
type CandidateError = { message: string; responseId?: string };

const USER_ABORT_MESSAGES: Record<string, true> = {
  "operation aborted": true,
  "request was aborted": true,
  "the operation was aborted": true,
  "this operation was aborted": true,
};

function recordName(target: string): string {
  const safe = [...target].map((char) => /[A-Za-z0-9._-]/.test(char) ? char : "_").join("");
  const digest = createHash("sha256").update(target, "utf8").digest("hex").slice(0, 16);
  return `${safe}-${digest}.json`;
}

function isUserAbortError(message: unknown): boolean {
  const normalized = String(message || "").trim().toLowerCase().replace(/[.!]+$/, "");
  return USER_ABORT_MESSAGES[normalized] === true;
}

function stateDir(): string {
  return process.env.TMUXBOT_STATE_DIR || join(process.env.HOME || "", ".local", "state", "tmuxbot");
}

function recordPath(target: string): string {
  return join(stateDir(), "omp-session-handoffs", recordName(target));
}

function healthPath(target: string): string {
  return join(stateDir(), "omp-session-health", recordName(target));
}

async function writeAtomic(path: string, payload: object): Promise<void> {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.${process.pid}.tmp`;
  await writeFile(temporary, `${JSON.stringify(payload)}\n`, { encoding: "utf8", mode: 0o600 });
  await rename(temporary, path);
}

export default function tmuxbotSessionHandoff(omp: ExtensionAPI): void {
  let target = "";
  let cwd = "";
  let sessionId = "";
  let transcriptPath = "";
  let candidateError: CandidateError | undefined;

  const writeHealth = async (state: HealthState): Promise<void> => {
    if (!target || !cwd || !sessionId || !transcriptPath) return;
    await writeAtomic(healthPath(target), {
      version: 1,
      tmuxTarget: target,
      cwd,
      sessionId,
      transcriptPath,
      state,
      observedAt: new Date().toISOString(),
      ...(state === "terminal_error" && candidateError ? { error: candidateError } : {}),
    });
  };

  const refreshIdentity = async (ctx: ExtensionContext): Promise<void> => {
    const pane = process.env.TMUX_PANE;
    const tmuxTarget = pane
      ? await omp.exec("tmux", ["display-message", "-p", "-t", pane, "#{session_name}:#{window_index}.#{pane_index}"], { timeout: 3000 })
      : undefined;
    const nextTarget = tmuxTarget?.code === 0 ? tmuxTarget.stdout.trim() : "";
    const sessionFile = ctx.sessionManager.getSessionFile();
    const nextSessionId = ctx.sessionManager.getSessionId();
    if (!nextTarget || !sessionFile || !nextSessionId) return;
    const nextCwd = await realpath(resolve(ctx.cwd));
    const nextTranscriptPath = resolve(sessionFile);
    await writeAtomic(recordPath(nextTarget), {
      version: 1,
      tmuxTarget: nextTarget,
      cwd: nextCwd,
      sessionId: nextSessionId,
      transcriptPath: nextTranscriptPath,
      processId: process.pid,
    });
    target = nextTarget;
    cwd = nextCwd;
    sessionId = nextSessionId;
    transcriptPath = nextTranscriptPath;
    candidateError = undefined;
    await writeHealth("idle");
  };

  omp.on("session_start", async (_event, ctx) => {
    await refreshIdentity(ctx);
  });

  omp.on("session_switch", async (_event, ctx) => {
    await refreshIdentity(ctx);
  });

  omp.on("agent_start", async () => {
    candidateError = undefined;
    await writeHealth("working");
  });

  omp.on("message_end", async (event) => {
    if (event.message.role !== "assistant") return;
    if (event.message.stopReason !== "error") {
      candidateError = undefined;
      return;
    }
    const message = String(event.message.errorMessage || "OMP provider request failed").slice(0, 500);
    if (isUserAbortError(message)) {
      candidateError = undefined;
      return;
    }
    candidateError = {
      message,
      ...(event.message.responseId ? { responseId: event.message.responseId } : {}),
    };
    await writeHealth("recovering");
  });

  omp.on("agent_end", async (event) => {
    const lifecycle = event as typeof event & { isTerminal?: boolean; willContinue?: boolean };
    if (lifecycle.isTerminal === false || lifecycle.willContinue === true) {
      await writeHealth(candidateError ? "recovering" : "working");
      return;
    }
    await writeHealth(candidateError ? "terminal_error" : "idle");
  });
}
