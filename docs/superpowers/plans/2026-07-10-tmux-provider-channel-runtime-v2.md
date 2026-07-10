# Tmux Provider and Channel Runtime V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor tmuxbot into a tmux-centric runtime where Claude/Codex providers and Telegram/Feishu channels communicate through normalized contracts.

**Architecture:** Tmux remains the execution plane. Provider adapters translate hooks, transcripts, and terminal captures into normalized events and statuses; the core reduces those into reply envelopes; channel adapters render the same envelopes for Telegram and Feishu.

**Tech Stack:** Python 3.10+, asyncio, tmux, pytest, aiogram, lark-oapi

## Global Constraints

- Every binding continues to operate a real Claude or Codex CLI inside tmux.
- Existing `bindings.yaml`, environment variables, commands, and active tmux sessions remain compatible.
- No SDK/API execution path replaces tmux.
- Claude and Codex must work with both Telegram and Feishu.
- All behavior changes follow test-first red/green cycles.
- Runtime V2 remains reversible until shadow and live parity are verified.
- Live smoke tests use dedicated test tmux sessions, never production bindings.

---

### Task 1: Add normalized core contracts

**Files:**
- Create: `tmuxbot/core/__init__.py`
- Create: `tmuxbot/core/events.py`
- Create: `tmuxbot/core/capabilities.py`
- Create: `tmuxbot/core/messages.py`
- Create: `tmuxbot/core/sessions.py`
- Create: `tmuxbot/core/replies.py`
- Create: `tests/test_core_contracts.py`

**Interfaces:**
- Produces: `ProviderEvent`, `ProviderEventKind`, `TerminalStatus`, `TerminalState`, `ProviderCapabilities`, `ChannelCapabilities`, `IncomingMessage`, `SessionIdentity`, and `ReplyEnvelope`.
- Consumes: no project runtime modules; these definitions remain dependency-light.

- [ ] **Step 1: Write failing contract tests**

```python
from tmuxbot.core.capabilities import ChannelCapabilities, ProviderCapabilities
from tmuxbot.core.events import ProviderEvent, ProviderEventKind, TerminalState, TerminalStatus
from tmuxbot.core.messages import IncomingMessage
from tmuxbot.core.replies import ReplyEnvelope
from tmuxbot.core.sessions import SessionIdentity


def test_core_contracts_are_provider_and_channel_neutral():
    status = TerminalStatus(
        state=TerminalState.IDLE,
        label="ready",
        model="gpt-5",
        permission_mode="yolo",
        cwd="/repo",
    )
    event = ProviderEvent(
        event_id="session:1",
        kind=ProviderEventKind.FINAL_TEXT,
        text="done",
        status=status,
    )
    reply = ReplyEnvelope(title="Reply", body=event.text, footer=status)
    incoming = IncomingMessage(source_id="chat", thread_id="topic", sender_id="boss", text="go")
    session = SessionIdentity(provider="codex", session_id="abc", transcript_path="/tmp/a.jsonl")

    assert reply.footer is status
    assert incoming.text == "go"
    assert session.provider == "codex"
    assert ProviderCapabilities(name="codex").name == "codex"
    assert ChannelCapabilities(name="telegram", supports_edit=True).supports_edit
```

- [ ] **Step 2: Run the test and verify RED**

Run: `python3 -m pytest tests/test_core_contracts.py -q`

Expected: import failure because `tmuxbot.core` does not exist.

- [ ] **Step 3: Implement immutable dataclasses and enums**

Use frozen dataclasses with explicit optional fields. `ProviderEventKind` includes `TEXT_DELTA`, `FINAL_TEXT`, `TOOL_PROGRESS`, `PLAN_UPDATE`, `INTERACTION_REQUEST`, `LIFECYCLE_CHANGE`, `USAGE_UPDATE`, and `PROVIDER_ERROR`. `TerminalState` includes `IDLE`, `WORKING`, `WAITING`, `BLOCKED`, and `DEAD`.

- [ ] **Step 4: Verify GREEN**

Run: `python3 -m pytest tests/test_core_contracts.py -q`

Expected: all contract tests pass.

- [ ] **Step 5: Commit**

```bash
git add tmuxbot/core tests/test_core_contracts.py
git commit -m "Add runtime v2 core contracts"
```

### Task 2: Formalize the provider adapter contract

**Files:**
- Modify: `tmuxbot/backends/base.py`
- Modify: `tmuxbot/backends/claude_code.py`
- Modify: `tmuxbot/backends/codex.py`
- Create: `tests/test_provider_contract.py`
- Modify: `tests/test_codex_backend.py`

**Interfaces:**
- Consumes: core contract classes from Task 1.
- Produces: provider capabilities, process detection, safe-start decisions, normalized terminal status, and footer formatting.

- [ ] **Step 1: Write failing provider contract tests**

```python
from tmuxbot.backends.claude_code import ClaudeCodeBackend
from tmuxbot.backends.codex import CodexBackend
from tmuxbot.core.events import TerminalState


def test_provider_process_detection_is_explicit():
    assert ClaudeCodeBackend().is_running_command("claude")
    assert CodexBackend().is_running_command("codex")
    assert CodexBackend().is_running_command("node")
    assert not ClaudeCodeBackend().is_running_command("python3")


def test_provider_status_parsers_normalize_terminal_chrome():
    claude = ClaudeCodeBackend().parse_terminal_status(
        "new task? /clear to save 387.4k tokens\n"
        "⏵⏵ accept edits on (shift+tab to cycle) · ← for agents"
    )
    codex = CodexBackend().parse_terminal_status(
        "• Working (9s • esc to interrupt)\n"
        "gpt-5.6-sol high · ~/repo"
    )

    assert claude is not None and claude.permission_mode == "accept edits"
    assert codex is not None and codex.state == TerminalState.WORKING
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m pytest tests/test_provider_contract.py -q`

Expected: missing provider contract methods.

- [ ] **Step 3: Add provider methods**

Extend `Backend` with:

```python
@property
def capabilities(self) -> ProviderCapabilities: ...

def is_running_command(self, command: str) -> bool: ...
def can_start_from_command(self, command: str) -> bool: ...
def parse_terminal_status(self, pane: str) -> TerminalStatus | None: ...
def format_status_footer(self, status: TerminalStatus | None) -> str | None: ...
```

Claude accepts native `claude` and known wrapper shapes discovered from its argv. Codex accepts `codex` and `node`. Both start only from `bash`, `zsh`, `sh`, or `fish`.

- [ ] **Step 4: Route lifecycle and heartbeat through the contract**

Replace direct `pane_command_name` equality checks with `is_running_command()`. Implement `find_tui_activity_fp()` as a compatibility wrapper around `parse_terminal_status()`.

- [ ] **Step 5: Verify GREEN and regressions**

Run: `python3 -m pytest tests/test_provider_contract.py tests/test_codex_backend.py -q`

Expected: provider contract and existing Codex lifecycle tests pass.

- [ ] **Step 6: Commit**

```bash
git add tmuxbot/backends tests/test_provider_contract.py tests/test_codex_backend.py
git commit -m "Normalize provider capabilities and terminal status"
```

### Task 3: Introduce a safe tmux runtime and per-pane input queues

**Files:**
- Create: `tmuxbot/runtime/__init__.py`
- Create: `tmuxbot/runtime/tmux_runtime.py`
- Modify: `tmuxbot/tmux.py`
- Modify: `tmuxbot/state.py`
- Create: `tests/test_tmux_runtime.py`
- Modify: `tests/test_lifecycle.py`

**Interfaces:**
- Consumes: provider process/status contract from Task 2.
- Produces: `TmuxRuntime.send_text()`, `TmuxRuntime.inspect()`, `TmuxRuntime.capture()`, and `TmuxRuntime.send_key()`.

- [ ] **Step 1: Write failing ordering and safety tests**

```python
def test_busy_pane_waits_before_paste(runtime, fake_tmux):
    fake_tmux.statuses = ["working", "working", "idle"]
    runtime.run_send("pane", "hello")
    assert fake_tmux.operations == ["inspect", "inspect", "inspect", "paste:hello", "key:Enter"]


def test_concurrent_messages_are_serialized(runtime, fake_tmux):
    runtime.run_many("pane", ["one", "two", "three"])
    assert fake_tmux.pasted == ["one", "two", "three"]


def test_unknown_foreground_process_rejects_launch(runtime, fake_tmux):
    fake_tmux.foreground = "python3"
    assert not runtime.safe_launch("pane", "codex --yolo", allowed_shells={"bash"})
    assert fake_tmux.pasted == []
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m pytest tests/test_tmux_runtime.py -q`

Expected: `TmuxRuntime` is missing.

- [ ] **Step 3: Implement queue-first input delivery**

Use one `asyncio.Lock` per tmux target. Inspect and wait before loading or pasting the buffer. Revalidate the provider process immediately before paste. Keep the existing `tmux_send_text()` function as a compatibility wrapper over the runtime singleton.

- [ ] **Step 4: Add pane inspection**

`TmuxPaneSnapshot` contains target, TTY, foreground command, pane PID, dead flag, and captured text. Use tmux formats and bounded `ps` argv inspection without shell interpolation.

- [ ] **Step 5: Verify GREEN and regression suite**

Run: `python3 -m pytest tests/test_tmux_runtime.py tests/test_lifecycle.py -q`

Expected: ordering, safety, and lifecycle tests pass.

- [ ] **Step 6: Commit**

```bash
git add tmuxbot/runtime tmuxbot/tmux.py tmuxbot/state.py tests/test_tmux_runtime.py tests/test_lifecycle.py
git commit -m "Add safe queued tmux runtime"
```

### Task 4: Pin provider session identity to bindings

**Files:**
- Modify: `tmuxbot/state.py`
- Modify: `tmuxbot/config.py`
- Modify: `tmuxbot/jsonl.py`
- Modify: `tmuxbot/backends/claude_code.py`
- Modify: `tmuxbot/backends/codex.py`
- Modify: `tmuxbot/provision.py`
- Create: `tests/test_session_identity.py`

**Interfaces:**
- Consumes: `SessionIdentity` from Task 1.
- Produces: exact `provider_session_id` and `transcript_path` binding state with bounded discovery fallback.

- [ ] **Step 1: Write failing same-cwd isolation tests**

Create two transcripts with the same cwd and different session IDs. Bind each test binding to a distinct ID and assert each backend resolves only its pinned transcript even when the other file has a newer mtime.

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m pytest tests/test_session_identity.py -q`

Expected: current mtime-based discovery selects the wrong file.

- [ ] **Step 3: Persist identity fields**

Add optional binding fields:

```python
provider_session_id: str | None = None
transcript_path: Path | None = None
```

Load and save them through binding configuration without breaking older YAML files.

- [ ] **Step 4: Pin transcript discovery**

Backends first validate the pinned path and session ID. Only when no valid pin exists may they perform cwd discovery. The tailer updates the pin when it observes a real session transition.

- [ ] **Step 5: Verify GREEN**

Run: `python3 -m pytest tests/test_session_identity.py tests/test_codex_backend.py -q`

Expected: same-cwd sessions remain isolated.

- [ ] **Step 6: Commit**

```bash
git add tmuxbot/state.py tmuxbot/config.py tmuxbot/jsonl.py tmuxbot/backends tmuxbot/provision.py tests/test_session_identity.py
git commit -m "Pin provider sessions to tmux bindings"
```

### Task 5: Migrate transcript parsing to normalized ProviderEvent values

**Files:**
- Modify: `tmuxbot/backends/base.py`
- Modify: `tmuxbot/backends/claude_code.py`
- Modify: `tmuxbot/backends/codex.py`
- Modify: `tmuxbot/jsonl.py`
- Create: `tmuxbot/core/event_reducer.py`
- Create: `tests/test_provider_events.py`
- Create: `tests/test_jsonl.py`
- Modify: existing backend tests

**Interfaces:**
- Consumes: `ProviderEvent` and core reply contracts.
- Produces: normalized events with stable deduplication IDs and a reducer-compatible stream.

- [ ] **Step 1: Write failing event normalization tests**

Test equivalent Claude and Codex final text, tool progress, plan update, and lifecycle entries. Assert both providers return the same event kinds and meaningful stable IDs.

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m pytest tests/test_provider_events.py -q`

Expected: current tuple events do not satisfy the normalized contract.

- [ ] **Step 3: Return ProviderEvent instances from both adapters**

Event IDs use provider session ID plus provider-native item ID when available, otherwise a SHA-256 digest of stable source fields. Preserve original source metadata for diagnostics.

- [ ] **Step 4: Add a compatibility reducer**

Move string-kind routing from `jsonl.on_tmux_event()` into `core.event_reducer`. The tailer reads provider events and dispatches reducer actions without provider-name branches.

- [ ] **Step 5: Verify GREEN**

Run: `python3 -m pytest tests/test_provider_events.py tests/test_codex_backend.py tests/test_jsonl.py -q`

Expected: normalized provider events and existing user-visible behavior pass.

- [ ] **Step 6: Commit**

```bash
git add tmuxbot/core/event_reducer.py tmuxbot/backends tmuxbot/jsonl.py tests
git commit -m "Normalize provider transcript events"
```

### Task 6: Make reply envelopes and enhanced replies channel-neutral

**Files:**
- Modify: `tmuxbot/frontends/base.py`
- Modify: `tmuxbot/frontends/telegram.py`
- Modify: `tmuxbot/frontends/feishu.py`
- Modify: `tmuxbot/replies.py`
- Modify: `tmuxbot/jsonl.py`
- Create: `tests/test_channel_reply_contract.py`
- Modify: `tests/test_telegram_replies.py`
- Create: `tests/test_feishu_replies.py`

**Interfaces:**
- Consumes: `ReplyEnvelope`, `TerminalStatus`, and `ChannelCapabilities`.
- Produces: required `ChannelAdapter.send_assistant_reply(binding, envelope)` behavior for both channels.

- [ ] **Step 1: Write failing channel parity tests**

Create one reply envelope with title, body, footer, attachment, and actions. Assert Telegram and Feishu render the same semantic text and each returns a platform message object suitable for later edits.

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m pytest tests/test_channel_reply_contract.py tests/test_feishu_replies.py -q`

Expected: Feishu lacks enhanced assistant replies and the base interface has no required method.

- [ ] **Step 3: Require the channel contract**

Add `capabilities` and abstract `send_assistant_reply()` to `Frontend`. Remove reflective `getattr()` dispatch from JSONL routing.

- [ ] **Step 4: Render provider-owned footer status**

Core reply assembly calls `backend.parse_terminal_status()` and attaches the normalized status to the envelope. Telegram and Feishu format it for their platform. Remove pane capture from Telegram reply sending and remove the generic last-line footer after compatibility tests migrate.

- [ ] **Step 5: Verify GREEN**

Run: `python3 -m pytest tests/test_channel_reply_contract.py tests/test_telegram_replies.py tests/test_feishu_replies.py -q`

Expected: Claude/Codex replies have channel parity and provider-specific normalized footers.

- [ ] **Step 6: Commit**

```bash
git add tmuxbot/frontends tmuxbot/replies.py tmuxbot/jsonl.py tests
git commit -m "Unify Telegram and Feishu reply envelopes"
```

### Task 7: Normalize inbound Telegram and Feishu messages

**Files:**
- Create: `tmuxbot/channels/__init__.py`
- Create: `tmuxbot/channels/base.py`
- Create: `tmuxbot/channels/telegram.py`
- Create: `tmuxbot/channels/feishu.py`
- Modify: `tmuxbot/frontends/telegram.py`
- Modify: `tmuxbot/frontends/feishu.py`
- Modify: `tmuxbot/addressing.py`
- Modify: `tmuxbot/attachments.py`
- Create: `tests/test_incoming_message_contract.py`

**Interfaces:**
- Consumes: `IncomingMessage` and `ChannelCapabilities`.
- Produces: channel-specific normalization into one provider-neutral inbound contract.

- [ ] **Step 1: Write failing inbound parity tests**

Build equivalent Telegram and Feishu source messages containing text, reply context, mention state,
and one attachment. Assert both adapters produce the same semantic `IncomingMessage` fields while
preserving their platform message IDs and source identities.

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m pytest tests/test_incoming_message_contract.py -q`

Expected: channel adapter modules are missing.

- [ ] **Step 3: Implement channel normalization adapters**

`TelegramChannelAdapter.normalize_incoming()` and `FeishuChannelAdapter.normalize_incoming()`
return `IncomingMessage`. Addressing policy consumes normalized `direct_chat`, `mentioned`, and
`replied_to_bot` flags. Attachment download remains platform-owned, but the resulting local
attachment descriptors are provider-neutral.

- [ ] **Step 4: Delegate legacy handlers to adapters**

Keep existing polling and SDK registration code in the frontend classes during migration. Replace
provider-facing message construction with adapter output so dispatch receives only
`IncomingMessage` fields.

- [ ] **Step 5: Verify GREEN**

Run: `python3 -m pytest tests/test_incoming_message_contract.py tests/test_telegram_mentions.py tests/test_feishu_mentions.py -q`

Expected: Telegram and Feishu inbound semantics match.

- [ ] **Step 6: Commit**

```bash
git add tmuxbot/channels tmuxbot/frontends tmuxbot/addressing.py tmuxbot/attachments.py tests/test_incoming_message_contract.py tests/test_telegram_mentions.py tests/test_feishu_mentions.py
git commit -m "Normalize Telegram and Feishu inbound messages"
```

### Task 8: Add managed Claude hook ingestion

**Files:**
- Create: `tmuxbot/hooks/__init__.py`
- Create: `tmuxbot/hooks/claude.py`
- Create: `tmuxbot/hooks/install.py`
- Modify: `tmuxbot/backends/claude_code.py`
- Modify: `tmuxbot/__main__.py`
- Create: `tests/test_claude_hooks.py`
- Add sanitized fixtures under: `tests/fixtures/claude_hooks/`

**Interfaces:**
- Consumes: Claude hook JSON on stdin and normalized provider events.
- Produces: append-only hook spool events for `SessionStart`, `Notification`, `MessageDisplay`, `TaskCreated`, `TaskCompleted`, `Stop`, and `StopFailure`.

- [ ] **Step 1: Write failing hook parsing and deduplication tests**

Use official-schema fixtures. Assert `Stop.last_assistant_message` becomes one `FINAL_TEXT` event and a later transcript copy is deduplicated. Assert `SessionStart` pins session ID and transcript path.

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m pytest tests/test_claude_hooks.py -q`

Expected: hook modules are missing.

- [ ] **Step 3: Implement a local hook spool**

`python -m tmuxbot.hooks.claude` reads one JSON document from stdin, validates supported fields, and appends one line to a lock-protected spool under the tmuxbot data directory. It never sends IM messages directly.

- [ ] **Step 4: Implement idempotent hook configuration**

The installer merges only tmuxbot-owned hook entries, preserves unrelated Claude settings, writes atomically, and supports dry-run output. Automatic installation is opt-in through `TMUXBOT_CLAUDE_HOOKS=true`.

- [ ] **Step 5: Consume hooks in the Claude adapter**

The Claude adapter tails the spool, emits normalized events, and uses hook session identity before transcript discovery. The JSONL tailer remains active for tool and usage events.

- [ ] **Step 6: Verify GREEN**

Run: `python3 -m pytest tests/test_claude_hooks.py -q`

Expected: official fixtures parse, settings merge is idempotent, and duplicate final replies are suppressed.

- [ ] **Step 7: Commit**

```bash
git add tmuxbot/hooks tmuxbot/backends/claude_code.py tmuxbot/__main__.py tests/test_claude_hooks.py tests/fixtures/claude_hooks
git commit -m "Add managed Claude hook ingestion"
```

### Task 9: Add Runtime V2 shadow mode and cutover

**Files:**
- Create: `tmuxbot/core/runtime_v2.py`
- Modify: `tmuxbot/__main__.py`
- Modify: `tmuxbot/jsonl.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `DEVELOPMENT.md`
- Create: `tests/test_runtime_v2.py`

**Interfaces:**
- Consumes: normalized provider, tmux runtime, and channel contracts.
- Produces: `TMUXBOT_RUNTIME_V2=off|shadow|on` rollout control and parity diagnostics.

- [ ] **Step 1: Write failing mode and parity tests**

Assert `off` uses legacy delivery, `shadow` computes V2 output without sending it, and `on` sends only V2 output. Assert shadow mismatches log redacted structural differences without user content leakage.

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m pytest tests/test_runtime_v2.py -q`

Expected: runtime mode router is missing.

- [ ] **Step 3: Implement mode routing**

Parse `TMUXBOT_RUNTIME_V2` with allowed values `off`, `shadow`, and `on`. Default to `off` until production shadow evidence passes.

- [ ] **Step 4: Add documentation and diagnostics**

Document rollout, rollback, hook opt-in, and dedicated smoke-session requirements. Log provider event kinds, reply shape hashes, and status-state differences without raw secrets.

- [ ] **Step 5: Verify GREEN**

Run: `python3 -m pytest tests/test_runtime_v2.py -q`

Expected: mode selection and shadow parity behavior pass.

- [ ] **Step 6: Commit**

```bash
git add tmuxbot/core/runtime_v2.py tmuxbot/__main__.py tmuxbot/jsonl.py .env.example README.md DEVELOPMENT.md tests/test_runtime_v2.py
git commit -m "Add runtime v2 shadow and cutover controls"
```

### Task 10: Full verification and dedicated tmux smoke matrix

**Files:**
- Create: `tests/e2e/test_tmux_provider_channel_matrix.py`
- Modify: `CHANGELOG.md`
- Modify: plan checkboxes in this document

**Interfaces:**
- Consumes: complete Runtime V2.
- Produces: automated fake-provider E2E coverage and documented manual live smoke evidence.

- [ ] **Step 1: Add fake CLI tmux E2E tests**

Create deterministic fake Claude and Codex terminal programs that emit sanitized transcript/status fixtures. Bind fake Telegram and Feishu channels and assert the 2×2 matrix receives equivalent reply envelopes.

- [ ] **Step 2: Run the full automated suite**

Run:

```bash
python3 -m pytest -q
python3 -m ruff check .
git diff --check
```

Expected: zero failures and zero lint or whitespace errors.

- [ ] **Step 3: Run dedicated live tmux smoke sessions**

Create non-production sessions named `tmuxbot-smoke-claude` and `tmuxbot-smoke-codex`. Verify input ordering, final text, footer, task/tool progress, restart reattachment, and same-cwd isolation. Do not reuse configured production binding panes.

- [ ] **Step 4: Shadow production bindings**

Run `TMUXBOT_RUNTIME_V2=shadow` for at least two lifecycle intervals per active binding. Require zero duplicate sends, zero launch injection, and no structural reply mismatches before enabling `on`.

- [ ] **Step 5: Enable V2 and verify rollback**

Enable `TMUXBOT_RUNTIME_V2=on`, restart the service, verify Telegram and Feishu health, then perform one controlled rollback to `off` and back to `on`.

- [ ] **Step 6: Update documentation and commit**

```bash
git add tests/e2e CHANGELOG.md docs/superpowers/plans/2026-07-10-tmux-provider-channel-runtime-v2.md
git commit -m "Complete tmux runtime v2 migration"
```

- [ ] **Step 7: Push the verified branch**

Run: `git push origin productization-prep`

Expected: local HEAD and `origin/productization-prep` resolve to the same commit.
