# Topic Routes and Admin DM

Status: accepted design; first implementation slice is on `feature/topic-router-pi` after v0.3.0.

## Product boundary

tmuxbot is a bidirectional transport between an exact IM endpoint and a real tmux pane. It moves user input, attachments, terminal state, structured local transcript events, and control keystrokes. It does not interpret development tasks or become a workflow/project-management system.

The same pane remains attachable over SSH. IM and SSH therefore control one shared interactive CLI/TUI session rather than separate headless jobs.

## Domain language

- **Endpoint**: `(channel, credential, chat_id, thread_id)` where `thread_id` is `int | str | None`. A Feishu topic route additionally persists `thread_root_message_id`, the durable `reply_in_thread=True` anchor used after bridge restarts and when output originates from direct tmux TUI interaction; a missing anchor fails closed and never falls back to the group root.
- **Route**: one persistent endpoint-to-target mapping.
- **Target**: `(tmux_session, tmux_window, tmux_pane, cwd)`.
- **Adapter**: provider-specific TUI and transcript behavior (`claude_code`, `codex`, `pi`).
- **Admin route**: a strictly ACL-protected DM route to a management pane.

A group is only a topic container. A group root with no explicit route never falls through to a project. Each bound topic maps to exactly one pane. Unbound topics remain silent even for legacy `/init`, `/projects`, and `/deinit`; topic routes are created explicitly through YAML, the route CLI, or Admin DM.

## Runtime model

```text
incoming IM event
  -> authenticate credential and Boss identity
  -> normalize exact endpoint
  -> resolve route
  -> resolve adapter from route.backend
  -> ensure/inspect the target pane
  -> inject into the real TUI

provider transcript/TUI event
  -> adapter normalizes event
  -> route identifies exact endpoint
  -> frontend sends to the same chat and topic/thread
```

A frontend owns one channel credential but may serve routes using different adapters. Adapter selection is per route, never per bot token.

## Configuration

The readable YAML remains the public route surface. Existing `bindings:` entries remain valid and become route records without a mandatory migration. The stable fields are:

```yaml
bindings:
  - name: repo-review
    channel: telegram
    bot_token_env: TG_BOT_TOKEN
    chat_id: -1001234567890
    thread_id: 42
    tmux_session: repo-review
    tmux_window: 0
    tmux_pane: 0
    cwd: /srv/repos/repo
    backend: pi
    mention_required: false
    cli_idle_timeout_seconds: 3600  # null inherits env; 0 keeps CLI resident
```

Uniqueness invariants:

1. route names are unique;
2. endpoints are unique, including credential and thread;
3. tmux targets are unique;
4. a group root and each topic are distinct endpoints;
5. when in-process reload lands, invalid candidates must not replace the last valid in-memory route table.

Direct file editing is allowed. `tmuxbot route validate` is available now; `tmuxbot route reload` is planned as a deterministic helper, not an authority boundary. Writes performed by tmuxbot use a temporary file, validate the complete candidate, then atomically replace YAML.

## Admin DM

The admin route is enabled explicitly. Its default target directory is `Path.home()`, overridden by `TMUXBOT_ADMIN_CWD`. Its CLI is configured by `TMUXBOT_ADMIN_CLI` and may be `pi`, `claude_code`, or `codex`. A persisted `admin: true` YAML record stores session identity only; without `TMUXBOT_ADMIN_ENABLED=1` it is ignored and cannot grant Admin capability.

Suggested environment:

```env
TMUXBOT_ADMIN_ENABLED=1
TMUXBOT_ADMIN_TMUX=tmuxbot-admin
TMUXBOT_ADMIN_CLI=pi
# TMUXBOT_ADMIN_CWD=/srv/projects
```

Admin capability is protected by both identity and endpoint shape:

- Telegram: Boss user ID, private chat, and the registered admin DM endpoint;
- Feishu: Boss open ID, `chat_type == p2p`, and the registered admin DM endpoint.

A group mention can never acquire admin capability. Once authenticated, the management TUI has the full permissions of the Unix account running tmuxbot; it may edit YAML, code, tmux, systemd user services, and other user-owned files.

The operator-facing natural-language workflow, prompt templates, deterministic execution order, and acceptance checklist are documented in [`admin-dm-operations.md`](admin-dm-operations.md). The recommended layout uses a root-directory Admin Pi for management and separate project panes for group topics.

Every Admin LLM receives the same **Admin Operations Contract** through a managed block installed into the Admin cwd's `AGENTS.md` and `CLAUDE.md`. The contract deliberately stays short: discover exact endpoints and panes, produce a plan, apply through a deterministic transaction, then verify. Provider-specific prompt skill is optional; correctness belongs to `tmuxbot admin`, not to the model's memory.

The transaction interface is:

```text
tmuxbot admin contract
tmuxbot admin install-contract --cwd PATH
tmuxbot admin inventory [--json]
tmuxbot admin telegram-topic --message-link URL [--json]
tmuxbot admin feishu-topics --env-file PATH --credential ENV --chat-id ID [--json]
# Feishu bind-topic/move-topic persist both thread_id and returned root_message_id.
tmuxbot admin bind-topic ... [--create-target] [--apply]
tmuxbot admin move-topic ROUTE ... [--apply]
tmuxbot admin verify ROUTE [--json]
```

`bind-topic` and `move-topic` are plan-only by default. `--apply` validates the complete candidate, atomically replaces YAML, restarts the explicitly named supervised bridge, and runs post-apply verification. Failure restores the old YAML and bridge; a newly created session is also removed. Existing tmux sessions are never destroyed by rollback.

## Route CLI

The deterministic management surface is:

```text
tmuxbot route list [--json]
tmuxbot route inspect NAME [--json]
tmuxbot route validate [--file PATH]
tmuxbot route bind ...
tmuxbot route attach ...
tmuxbot route unbind NAME
tmuxbot route reload
tmuxbot route stop NAME
tmuxbot route restart NAME
tmuxbot route replace-cli NAME BACKEND
```

The route-store slice provides `list`, `inspect`, `validate`, `bind`, and `unbind`, plus per-route adapter dispatch. The Admin transaction layer now owns the common create/move/verify workflow and supervised restart. `attach`, in-process reload, and broader lifecycle operations remain planned behind the route namespace; direct YAML edits still require an explicit bridge restart.

## Pi adapter

Pi runs in its interactive TUI in tmux. tmuxbot does not use Pi RPC, SDK, or print mode. The adapter:

- launches `PI_BIN` (default `pi`);
- resumes the latest matching local session when a persisted identity exists;
- discovers transcripts under `~/.pi/agent/sessions/--<encoded-cwd>--/*.jsonl`;
- validates the session header cwd before claiming a transcript;
- normalizes assistant text, thinking, and tool calls;
- reads model/thinking/usage metadata;
- recognizes Pi TUI process and activity state, preserves Pi's native Working-state steering queue, and verifies cold wake before delivering: ordinary text and attachments are submitted immediately while streaming; a shell wake ignores stale TUI scrollback but must observe a real Pi footer/status before the user payload is pasted;
- mirrors Pi's native footer or the installed `pi-statusline` powerline fields: provider/model/thinking, cumulative input/output, cacheRead (`R`)/cacheWrite (`W`), latest prompt cache-hit rate, cost/subscription, context usage/window and auto-compaction, cwd/Git branch/session name, plus extension status lines. TUI values remain the real-time source; the active Pi JSONL only fills fields omitted by terminal width. Claude/Codex retain their existing provider-specific status parsers;
- replays `rpiv-todo` from the active JSONL branch's latest valid `todo` tool-result snapshot. Whenever at least one non-deleted task exists, every Pi assistant/working IM message carries a persistent TUI-style `Todos (completed/total)` panel with snapshot order, status glyphs, task IDs, `activeForm`, and dependency IDs. Completed-only snapshots remain visible; the panel disappears only after clear/all-deleted. Deleted tasks and abandoned JSONL branches are ignored;
- mirrors auto-compaction as an editable IM status: TUI `Auto-compacting...` starts a countdown based on the median of the session's latest compaction durations (default 180 seconds), updates periodically, and JSONL `type=compaction` finalizes the card with token metadata. If the TUI leaves compaction without the hard JSONL marker, the card fails closed and warns that work may not have resumed;
- treats Telegram final-response delivery as part of transcript consumption: every split message must return a Bot API `message_id`; otherwise the current JSONL row keeps its offset and is retried. Successful delivery logs all message IDs. This is at-least-once retry, not yet a durable idempotent outbox;
- when a source checkout is operated through `~/.local/bin/tmuxbot`, `bin/restart.sh` force-reinstalls that checkout's full uv tool before restarting systemd, so route behavior cannot silently lag behind the reviewed Git revision.

For reliable modified-key handling Pi recommends tmux `extended-keys on` and `extended-keys-format csi-u`. Doctor may diagnose this but must not kill or restart the user's tmux server.

## Compatibility rollout

1. Widen endpoint thread IDs and validation while preserving existing YAML.
2. Add an adapter registry to each frontend and resolve adapters per binding.
3. Add Pi as a third adapter.
4. Add route CLI and atomic route-store operations.
5. Add strict Admin DM provisioning and hot reload.
6. Complete Feishu threaded outbound probes and enable thread replies.

At every stage old one-group/one-project bindings continue to run. A message with no exact endpoint match remains silent and never touches tmux.
