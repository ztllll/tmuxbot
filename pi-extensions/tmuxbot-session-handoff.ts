import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createHash } from "node:crypto";
import { mkdir, rename, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";

function recordName(target: string): string {
  const safe = [...target].map((char) => /[A-Za-z0-9._-]/.test(char) ? char : "_").join("");
  const digest = createHash("sha256").update(target, "utf8").digest("hex").slice(0, 16);
  return `${safe}-${digest}.json`;
}

function stateDir(): string {
  return process.env.TMUXBOT_STATE_DIR || join(process.env.HOME || "", ".local", "state", "tmuxbot");
}

function recordPath(target: string): string {
  return join(stateDir(), "pi-session-handoffs", recordName(target));
}

function healthPath(target: string): string {
  return join(stateDir(), "pi-session-health", recordName(target));
}

async function writeAtomic(path: string, payload: object): Promise<void> {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.${process.pid}.tmp`;
  await writeFile(temporary, `${JSON.stringify(payload)}\n`, { encoding: "utf8", mode: 0o600 });
  await rename(temporary, path);
}

export default function tmuxbotSessionHandoff(pi: ExtensionAPI): void {
  let target = "";
  let cwd = "";
  let sessionId = "";
  let transcriptPath = "";
  let candidateError: { message: string; responseId?: string } | undefined;

  const writeHealth = async (state: "idle" | "working" | "recovering" | "terminal_error"): Promise<void> => {
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

  pi.on("session_start", async (_event, ctx) => {
    const tmuxTarget = process.env.TMUX_PANE
      ? await pi.exec("tmux", ["display-message", "-p", "-t", process.env.TMUX_PANE, "#{session_name}:#{window_index}.#{pane_index}"], { timeout: 3000 })
      : undefined;
    target = tmuxTarget?.code === 0 ? tmuxTarget.stdout.trim() : "";
    const sessionFile = ctx.sessionManager.getSessionFile();
    const id = ctx.sessionManager.getSessionId();
    if (!target || !sessionFile || !id) return;
    cwd = resolve(ctx.cwd);
    sessionId = id;
    transcriptPath = resolve(sessionFile);
    candidateError = undefined;

    await writeAtomic(recordPath(target), {
      version: 1,
      tmuxTarget: target,
      cwd,
      sessionId,
      transcriptPath,
    });
    await writeHealth("idle");
  });

  pi.on("agent_start", async () => {
    candidateError = undefined;
    await writeHealth("working");
  });

  pi.on("message_end", async (event) => {
    if (event.message.role !== "assistant") return;
    if (event.message.stopReason === "error") {
      candidateError = {
        message: String(event.message.errorMessage || "Pi provider request failed").slice(0, 500),
        ...(event.message.responseId ? { responseId: event.message.responseId } : {}),
      };
      await writeHealth("recovering");
      return;
    }
    candidateError = undefined;
  });

  pi.on("agent_settled", async () => {
    await writeHealth(candidateError ? "terminal_error" : "idle");
  });
}
