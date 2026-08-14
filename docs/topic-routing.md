# Topic Routes and Admin DM

Status: implemented on `main`; v0.3.0 is the frozen baseline and v0.3.1 is the OMP native-adapter release.

## Product boundary

tmuxbot is a bidirectional transport between an exact IM endpoint and a real tmux pane. It moves user input, attachments, terminal state, structured local transcript events, and provider-safe controls. It does not interpret development tasks or become a workflow/project-management system. OMP native menus, confirmations, and pickers are deliberately observation-only: live interactions are detected and handed off to SSH rather than translated into IM keystrokes. Bot `/plan` is local help and is never injected as an OMP slash command.

The same pane remains attachable over SSH. IM and SSH therefore control one shared interactive CLI/TUI session rather than separate headless jobs.

## Domain language

- **Endpoint**: `(channel, credential, chat_id, thread_id)` where `thread_id` is `int | str | None`. A Feishu topic route additionally persists `thread_root_message_id`, the durable `reply_in_thread=True` anchor used after bridge restarts and when output originates from direct tmux TUI interaction; a missing anchor fails closed and never falls back to the group root.
- **Route**: one persistent endpoint-to-target mapping.
- **Target**: `(tmux_session, tmux_window, tmux_pane, cwd)`.
- **Adapter**: provider-specific TUI and transcript behavior (`claude_code`, `codex`, `omp`).
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

The readable YAML remains the public route surface. The `bindings:` record shape remains stable; legacy records must be migrated to a current backend key rather than relying on aliases. The stable fields are:

```yaml
bindings:
  - name: repo-review
    channel: telegram
    bot_token_env: TG_OMP_BOT_TOKEN
    chat_id: -1001234567890
    thread_id: 42
    tmux_session: repo-review
    tmux_window: 0
    tmux_pane: 0
    cwd: /srv/repos/repo
    backend: omp
    mention_required: false
```

Uniqueness invariants:

1. route names are unique;
2. endpoints are unique, including credential and thread;
3. tmux targets are unique;
4. a group root and each topic are distinct endpoints;
5. when in-process reload lands, invalid candidates must not replace the last valid in-memory route table.

Direct file editing is allowed. `tmuxbot route validate` is available now; `tmuxbot route reload` is planned as a deterministic helper, not an authority boundary. Writes performed by tmuxbot use a temporary file, validate the complete candidate, then atomically replace YAML.

## Admin DM

The admin route is enabled explicitly. Its default target directory is the dedicated XDG workspace `$XDG_DATA_HOME/tmuxbot/admin` (normally `~/.local/share/tmuxbot/admin`), overridden by `TMUXBOT_ADMIN_CWD`. tmuxbot creates it with mode `0700`. Its CLI is configured by `TMUXBOT_ADMIN_CLI` and may be `omp`, `claude_code`, or `codex`. A persisted `admin: true` YAML record stores session identity only; without `TMUXBOT_ADMIN_ENABLED=1` it is ignored and cannot grant Admin capability.

Suggested environment:

```env
TMUXBOT_ADMIN_ENABLED=1
TMUXBOT_ADMIN_TMUX=tmuxbot-admin
TMUXBOT_ADMIN_CLI=omp
# TMUXBOT_ADMIN_CWD=/srv/tmuxbot-admin
```

Admin capability is protected by both identity and endpoint shape:

- Telegram: Boss user ID, private chat, and the registered admin DM endpoint;
- Feishu: Boss open ID, `chat_type == p2p`, and the registered admin DM endpoint.

A group mention can never acquire admin capability. Once authenticated, the management TUI has the full permissions of the Unix account running tmuxbot; it may edit YAML, code, tmux, systemd user services, and other user-owned files.

The operator-facing natural-language workflow, prompt templates, deterministic execution order, and acceptance checklist are documented in [`admin-dm-operations.md`](admin-dm-operations.md). The recommended layout uses a dedicated Admin workspace outside both the home-directory root and project trees, plus separate project panes for group topics.

Every Admin LLM receives the same **Admin Operations Contract** through a managed block installed into the Admin cwd's `AGENTS.md` and `CLAUDE.md`. `install-contract` also writes `ADMIN-RUNBOOK.md` with deployment-specific runtime knowledge and `tmuxbot-admin-context.json` with schema version and content hashes. `verify-context` detects missing, modified, or stale context. The contract stays short and mandatory; the runbook supplies progressive detail. Provider-specific system prompts or skills are optional hardening, while correctness belongs to `tmuxbot admin`, not to model memory.

The transaction interface is:

```text
tmuxbot admin contract
tmuxbot admin install-contract [--cwd PATH]
tmuxbot admin verify-context [--cwd PATH] [--json]
tmuxbot admin provision-project --name ROUTE --channel telegram|feishu ... [--apply]
# Lower-level recovery/diagnostics:
tmuxbot admin inventory [--json]
tmuxbot admin telegram-topic --message-link URL [--json]
tmuxbot admin feishu-topics --env-file PATH --credential ENV --chat-id ID [--json]
tmuxbot admin create-topic --env-file PATH --channel telegram|feishu ... [--create-target] [--apply]
# Feishu create/bind/move persist thread_id + root_message_id; Telegram needs only chat_id + thread_id.
tmuxbot admin bind-topic ... [--create-target] [--apply]
tmuxbot admin move-topic ROUTE ... [--apply]
tmuxbot admin verify ROUTE [--json]
# Recovery only after an operator directly switched the running OMP TUI session:
# validate the exact session header + cwd, review plan, then repeat with --apply.
tmuxbot admin adopt-omp-session ROUTE --session-file /absolute/omp-session.jsonl [--apply]
```

`provision-project` is the normal deep interface and is plan-only by default. It accepts one topic intent—create by title, bind a Telegram topic URL, or bind exact chat/thread IDs—then owns endpoint resolution, exact-cwd target creation/reuse, candidate validation, atomic write, supervised restart, verification, and rollback. Its default target is `NAME:0.0`, so callers do not need to coordinate separate discovery/create/bind commands. `create-topic`, `bind-topic`, and `move-topic` remain plan-only low-level recovery interfaces. The create command exists for the common Admin-DM request “create this named Telegram/Feishu topic, create this tmux session in this cwd, and bind OMP/Claude/Codex”: one reviewed command owns the channel API result, target creation, route write, supervised restart, and verification. Failure restores the old YAML and bridge, removes a transaction-created session, and attempts to delete the transaction-created topic. Existing topics and tmux sessions are never destroyed by rollback. Telegram topic URLs may be `https://t.me/c/CHAT/THREAD` or include an optional message ID; Telegram never requires a durable root-message anchor. An OMP route normally follows provider-authored sidecar handoffs. If an operator switches the live OMP TUI outside the channel flow and outbound replies stop because the route remains pinned to the former JSONL, use `adopt-omp-session` with the exact new absolute session path: it validates the first usable `type="session"` header, session ID, supported version, and canonical route cwd; plans before writing; atomically persists the new identity; restarts the supervised bridge; and verifies the route. It never discovers or guesses a session by mtime.

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

The route-store slice provides `list`, `inspect`, `validate`, `bind`, and `unbind`, plus per-route adapter dispatch. The Admin transaction layer owns the common create/move/verify workflow and supervised restart. The broader command list above records the intended namespace; commands not exposed by `tmuxbot route --help` remain planned. Direct YAML edits still require an explicit bridge restart.

## OMP adapter

`OmpBackend` runs Oh My Pi as the real interactive TUI in tmux. tmuxbot does not use OMP RPC, SDK, print, or headless modes. The current contract is:

- the route backend is exactly `omp`; no compatibility backend alias is registered;
- provider discovery resolves `OMP_BIN`, then `PATH`, then `~/.local/bin/omp`; the registry owns display name `Oh My Pi`, default Telegram credential `TG_OMP_BOT_TOKEN`, and launch argv. Web/API clients may select provider ID but cannot submit a binary path, tmux target, or argv;
- a managed launch is exactly `omp --approval-mode yolo --extension <managed-extension-absolute-path>`. A pinned route appends only `--resume <exact-absolute-jsonl-path>`; invalid/mismatched pins are preserved and launch fails closed rather than silently starting a replacement session;
- transcripts are OMP JSONL v3 under `~/.omp/agent/sessions/...`, but discovery does not scan that tree. `omp-session-handoffs/` records exact target, canonical cwd, session ID, transcript path, and live pane process ID; `omp-session-health/` records the same session identity and health state. Before a brand-new session creates its JSONL, only an official sessions-root path with matching session ID and process in the exact pane is accepted; once the file exists, header/cwd/session validation is mandatory. A live OMP process without a valid managed identity sidecar is not safe for IM injection;
- the current branch is reconstructed from the last entry through `id/parentId`, excluding title/session slots and abandoned branches. Metadata comes from `model_change.model="provider/model"`, `thinking_level_change.thinkingLevel`, title/header/title changes, and assistant usage/cost; assistant provider/model fields are fallback only;
- assistant `thinking`, `toolCall`, and `text` blocks normalize to tool progress and final text. Only a non-empty `write` tool call targeting `local://*-plan.md` emits a plan update. Plan mode itself is the current branch's last `mode_change.mode`; no third-party plan-mode or statusline extension is part of the contract;
- todo state is the newest current-branch snapshot from successful `toolResult(toolName="todo").details.phases` or `custom(customType="user_todo_edit").data.phases`. Tasks support `pending`, `in_progress`, `completed`, `abandoned`, and `blocked`, with optional `blocker`; rendering groups by phase and omits abandoned tasks;
- ordinary text and attachments may enter OMP's native busy steering queue. Control commands and picker/menu commands require IDLE and fail immediately while busy. `/restart` performs a clean pane respawn and exact-path resume instead of injecting C-c/C-d;
- OMP 17.3.2's native `╭── π … ╮` / `╰─ … ─╯` footer pair and adjacent braille loader ending in `⟦esc⟧` are weak, versioned screen signals only. JSONL/sidecars remain authoritative; the footer may contribute only unambiguous effort, cwd, branch, context, cost, and plan/session labels;
- ask, approval, model/resume picker, plan review, and other native interactions are accepted only when their controls are adjacent to the live footer. IM receives one exact SSH/tmux target notice and never injects navigation, approval, confirmation, or cancellation keys. `/plan` only reports local mode status/help and suggests the default `Alt+Shift+P` over SSH when inactive;
- canonical `type="compaction"` or an extension session-switch lifecycle is the completion signal. `tokensBefore` maps to `preTokens`; absent official fields remain `postTokens=None` and `durationMs=None`.

For reliable modified-key handling OMP recommends tmux `extended-keys on` and `extended-keys-format csi-u`. Doctor may diagnose this but must not kill or restart the user's tmux server.

## Compatibility rollout

1. Widen endpoint thread IDs and validation while preserving existing YAML.
2. Add an adapter registry to each frontend and resolve adapters per binding.
3. Add OMP as the third adapter and cleanly cut over route/runtime vocabulary.
4. Add route CLI and atomic route-store operations.
5. Add strict Admin DM provisioning and hot reload.
6. Complete Feishu threaded outbound probes and enable thread replies.

At every stage one-group/one-project routes using current backend keys continue to run. A message with no exact endpoint match remains silent and never touches tmux.
