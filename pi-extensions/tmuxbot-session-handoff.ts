import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createHash } from "node:crypto";
import { mkdir, rename, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";

function recordPath(target: string): string {
  const stateDir = process.env.TMUXBOT_STATE_DIR || join(process.env.HOME || "", ".local", "state", "tmuxbot");
  const safe = [...target].map((char) => /[A-Za-z0-9._-]/.test(char) ? char : "_").join("");
  const digest = createHash("sha256").update(target, "utf8").digest("hex").slice(0, 16);
  return join(stateDir, "pi-session-handoffs", `${safe}-${digest}.json`);
}

export default function tmuxbotSessionHandoff(pi: ExtensionAPI): void {
  pi.on("session_start", async (_event, ctx) => {
    const tmuxTarget = process.env.TMUX_PANE
      ? await pi.exec("tmux", ["display-message", "-p", "-t", process.env.TMUX_PANE, "#{session_name}:#{window_index}.#{pane_index}"], { timeout: 3000 })
      : undefined;
    const target = tmuxTarget?.code === 0 ? tmuxTarget.stdout.trim() : "";
    const transcriptPath = ctx.sessionManager.getSessionFile();
    const sessionId = ctx.sessionManager.getSessionId();
    if (!target || !transcriptPath || !sessionId) return;

    const path = recordPath(target);
    const payload = JSON.stringify({
      version: 1,
      tmuxTarget: target,
      cwd: resolve(ctx.cwd),
      sessionId,
      transcriptPath: resolve(transcriptPath),
    }) + "\n";
    await mkdir(dirname(path), { recursive: true, mode: 0o700 });
    const temporary = `${path}.${process.pid}.tmp`;
    await writeFile(temporary, payload, { encoding: "utf8", mode: 0o600 });
    await rename(temporary, path);
  });
}
