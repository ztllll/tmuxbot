# Tmux-Centric Provider and Channel Runtime V2 Design

## Status

Approved interactively on 2026-07-10.

## Objective

Refactor tmuxbot into a tmux-centric runtime with two independent adapter axes:

- providers: Claude Code and Codex CLI;
- channels: Telegram and Feishu.

The refactor must preserve the current operating model: the real Claude or Codex CLI continues to
run inside a tmux pane, and Telegram or Feishu remotely controls that exact live terminal session.
Hooks and transcript files improve observation but never replace tmux as the execution plane.

## Core Invariant

Tmux remains the source of execution truth.

- Every binding maps to a real tmux pane.
- Claude and Codex remain native interactive CLI processes inside that pane.
- User input is delivered through tmux paste and key operations.
- Escape, approval, picker, arrow-key, and other interactions operate the real TUI.
- The bot may stop and restart without terminating the CLI session.
- SDK-backed execution, per-message subprocess execution, and provider-native chat channels are not
  the primary runtime path.

The architecture can be summarized as:

```text
Telegram ─┐                     ┌─ Claude Adapter
          ├─ Channel Gateway ─ Core Runtime ─┤
Feishu ───┘                     └─ Codex Adapter
                         │
                    Tmux Runtime
```

## Current Problems

### Provider details leak into channel code

Telegram captures the tmux pane and derives a footer by selecting the last non-empty terminal line.
Feishu does not implement the enhanced assistant-reply path at all and falls back to plain message
sending. The resulting feature set depends on both the provider and the channel instead of on an
explicit capability contract.

Claude and Codex have different terminal chrome. A generic last-line parser cannot reliably
interpret both. The current behavior happens to produce a useful Codex footer but often selects a
Claude keyboard hint such as `accept edits on` rather than a normalized session status.

### Provider lifecycle checks are inconsistent

Codex now accepts both `node` and `codex` process shapes and starts only from a known shell. Claude
still compares a single process name and can inject a launch command when the foreground process is
unexpected. Provider detection, safe launch, and readiness must be formal backend operations.

### Session discovery can select the wrong transcript

Provider session discovery primarily scans by working directory and modification time. Concurrent
sessions in the same directory can cause a binding to follow another pane's transcript. A binding
must persist the exact provider session ID and transcript path associated with its tmux pane.

### Input is pasted before the pane is ready

The current tmux input path pastes text and then waits for the TUI to become idle before pressing
Enter. Repeated watchdog or user inputs can therefore accumulate in the live editor. The runtime
must queue first, wait for a safe input state, then paste and submit exactly once.

### Large channel modules own too many responsibilities

Telegram and Feishu implementations combine platform transport, access control, provisioning,
attachments, command handling, provider-specific behavior, and reply presentation. Provider and
channel differences are therefore difficult to test independently.

## Architecture

### 1. Tmux Runtime

`TmuxRuntime` is the execution-plane port. It owns:

- session, window, pane, TTY, foreground PGID, and argv discovery;
- pane capture and terminal key operations;
- serialized per-pane input queues;
- safe launch from recognized shells only;
- process liveness and provider detection;
- lifecycle state: `idle`, `working`, `waiting`, `blocked`, and `dead`;
- stable attachment to panes that predate the bot process.

The central runtime entity is:

```text
TmuxSession
├── target: session/window/pane
├── cwd
├── tty
├── foreground process and PGID
├── provider
├── provider session ID
├── transcript path
├── input queue
└── lifecycle state
```

Input submission follows this order:

1. enqueue the complete message;
2. wait until the pane accepts normal input;
3. verify the foreground process is still the expected provider;
4. paste the complete payload once;
5. send Enter once;
6. record submission state and release the queue for the next message.

Launch commands may only be injected when the foreground process is a recognized interactive
shell. An unknown foreground process is left untouched and produces a structured warning.

### 2. Provider Adapters

`ProviderAdapter` understands one CLI but does not know about Telegram or Feishu. Claude and Codex
implement the same contract.

Provider output is normalized into:

```text
ProviderEvent
├── TextDelta
├── FinalText
├── ToolProgress
├── PlanUpdate
├── InteractionRequest
├── LifecycleChange
├── UsageUpdate
└── ProviderError
```

Each provider declares `ProviderCapabilities`, including:

- hooks;
- structured transcript;
- incremental text;
- resume and continue;
- task tracking;
- plan updates;
- usage and quota information;
- interactive picker support;
- terminal status parsing.

Each adapter also produces `TerminalStatus`:

```text
TerminalStatus
├── state: idle | working | waiting | blocked | dead
├── label
├── model
├── effort
├── permission mode
├── cwd
├── duration
├── context usage
└── blocked reason
```

#### Claude Adapter

Claude uses these sources in priority order:

1. hooks for session identity, turn completion, notifications, tasks, and interaction state;
2. JSONL for tools, thinking, usage, history, and additional structured content;
3. terminal capture for live status and interactive UI fallback.

The `Stop` hook's `last_assistant_message` is the preferred final-answer source because Claude's
official documentation states that transcript writes are asynchronous and can lag behind hook
execution. Hook and JSONL events are deduplicated before delivery.

#### Codex Adapter

Codex uses these sources in priority order:

1. rollout JSONL for deltas, final text, tools, plans, and usage;
2. terminal capture for status, approval UI, pickers, and startup dialogs;
3. polling only as a recovery path when no structured event is available.

### 3. Core Runtime

The core is independent of provider and channel names. It owns:

- binding and session routing;
- exact provider-session identity;
- input queue orchestration;
- normalized event reduction;
- event IDs and deduplication;
- tool-progress aggregation;
- plan-message lifecycle;
- streaming reply lifecycle;
- final reply assembly;
- retry and recovery decisions.

The core converts `ProviderEvent` values into `ReplyEnvelope` values:

```text
ReplyEnvelope
├── title
├── body
├── footer: TerminalStatus | None
├── attachments
├── actions
├── replace/edit key
└── notification policy
```

A provider status is attached to the reply before it reaches a channel. Channel code never captures
the pane to guess a footer.

### 4. Channel Adapters

`ChannelAdapter` understands one messaging platform but does not know Claude or Codex schema.

Inbound platform events become `IncomingMessage` values containing:

- source chat and thread identity;
- sender identity;
- text or caption;
- reply relationship;
- mentions;
- downloaded attachments;
- platform message ID;
- command metadata.

Each channel declares `ChannelCapabilities`, including:

- message editing;
- buttons or interactive cards;
- threads or topics;
- native images and files;
- typing indicators;
- rich text limits;
- reply relationships.

Both Telegram and Feishu receive the same `ReplyEnvelope`. Their only difference is presentation:
Telegram may render HTML and inline keyboards, while Feishu may render cards and card actions.

`send_assistant_reply` becomes a required channel operation. Runtime reflection through
`getattr(frontend, "send_assistant_reply", None)` is removed.

## Data Flow

### Inbound

```text
Telegram / Feishu
→ IncomingMessage
→ Binding Router
→ Session Input Queue
→ Tmux Runtime
→ Claude / Codex pane
```

Each binding has one ordered queue. A busy pane queues input without modifying the live terminal
editor. Text, images, files, captions, and reply context follow the same normalized path.

### Outbound

```text
Hooks / JSONL / Terminal
→ Provider Adapter
→ ProviderEvent
→ Event Reducer
→ ReplyEnvelope
→ Telegram / Feishu Renderer
```

## Session Identity

Bindings persist:

- provider name;
- provider session ID;
- transcript path;
- tmux target;
- cwd;
- last verified foreground process identity.

`/new`, `/clear`, and `/resume` must produce explicit session-transition events. Discovery by cwd
and newest modification time remains only a bounded recovery mechanism. Multiple sessions using the
same cwd must not share transcript offsets or outbound events.

## Footer and Status Behavior

Footer content is provider-owned and channel-neutral:

```text
Provider Adapter → TerminalStatus → ReplyEnvelope.footer
```

Examples:

- Claude: `Fable 5 · accept edits · 383.6k/1m`
- Codex: `gpt-5.6-sol high · YOLO · ~/project`
- working: normalized stage, duration, and blocked reason
- unavailable: omit the footer rather than guessing from arbitrary terminal text

The old generic last-non-empty-line footer is removed after migration.

## Error Handling and Recovery

- Every structured event receives a stable deduplication key.
- Hook, transcript, and terminal observations of the same reply produce one outbound message.
- Unknown provider schema generates a structured warning and skips only the malformed event.
- Channel sends use bounded retry; failed edits fall back to a new message.
- Tailers recover from truncated or rotated transcript files without replaying acknowledged events.
- Dead panes produce an explicit lifecycle event and recovery actions.
- The runtime never launches over an unknown foreground process.
- Runtime V2 can be disabled to restore the legacy path during rollout.

## Migration Strategy

### Phase 1: Characterize the current system

- Add regression tests for existing commands, replies, attachments, provisioning, and recovery.
- Store sanitized Claude/Codex transcript, hook, and terminal fixtures.
- Preserve existing binding YAML, environment variables, and bot commands.

### Phase 2: Introduce core contracts

Add focused modules:

```text
tmuxbot/core/
├── events.py
├── capabilities.py
├── messages.py
├── sessions.py
├── reply_state.py
└── input_queue.py

tmuxbot/runtime/
└── tmux_runtime.py

tmuxbot/providers/
├── base.py
├── claude.py
└── codex.py

tmuxbot/channels/
├── base.py
├── telegram.py
└── feishu.py
```

Legacy modules remain operational through temporary adapters.

### Phase 3: Migrate the tmux execution plane

- Add foreground-process and PGID detection.
- Move capture, key operations, lifecycle checks, and input queues into `TmuxRuntime`.
- Persist exact session identity.

### Phase 4: Migrate providers

Migrate Codex first as the reference adapter, then Claude with managed minimal hooks. Provider
behavior moves out of channel code and string comparisons become capability or adapter operations.

### Phase 5: Migrate channels

Move reply rendering, attachments, edit behavior, actions, and addressing onto the channel contract.
Telegram and Feishu use the same reply and streaming state machines.

### Phase 6: Shadow and cut over

Introduce:

```text
TMUXBOT_RUNTIME_V2=true
```

Before sending through V2, run it in shadow mode and compare normalized events and reply envelopes
against the legacy runtime. After parity is established, enable V2 delivery and retain an immediate
rollback switch. Remove legacy paths only after a stable observation period.

## Testing Strategy

The required contract matrix is:

| Provider | Telegram | Feishu |
|---|---:|---:|
| Claude | unit, contract, tmux E2E | unit, contract, tmux E2E |
| Codex | unit, contract, tmux E2E | unit, contract, tmux E2E |

Mandatory regression scenarios:

- ten messages sent while a pane is busy remain ordered and unmerged;
- multiple sessions in one cwd do not cross streams;
- bot restart reattaches to existing tmux sessions;
- Claude and Codex footer golden tests;
- hook and JSONL duplicates produce one message;
- Telegram edit failure and Feishu card-update failure degrade safely;
- unknown foreground processes never receive launch text;
- runtime V2 disabled restores legacy behavior;
- sanitized fixture tests run without live provider credentials;
- live smoke tests use dedicated tmux sessions and never the production bindings.

## External Lessons Applied

- CCGram demonstrates a provider protocol, capability descriptor, normalized agent messages, and
  provider-specific terminal status parsing while keeping tmux as the terminal source of truth.
- Claude's official hook contract provides exact session IDs, transcript paths, completion events,
  task events, notifications, and final assistant text.
- Telegram AI Agent separates engine, execution mode, and stream mode at configuration boundaries.
- Claude Channels demonstrates allowlists and permission relay, but is not adopted as the primary
  transport because it is Claude-specific, does not cover Feishu, and is currently a research
  preview.

References:

- https://github.com/alexei-led/ccgram
- https://github.com/alexei-led/ccgram/blob/main/src/ccgram/providers/base.py
- https://github.com/pavel-molyanov/telegram-ai-agent
- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/channels

## Compatibility Requirements

- Existing `bindings.yaml` entries continue to load.
- Existing Telegram and Feishu credentials and token environment variables continue to work.
- Existing bot commands remain available unless separately deprecated.
- Existing tmux sessions remain attachable and are not recreated during migration.
- Runtime V2 does not require Claude or Codex SDK credentials beyond the existing CLI login state.

## Non-Goals

This refactor does not add:

- Gemini or another provider;
- Discord, Slack, or another channel;
- SDK/API execution mode;
- a web dashboard;
- multi-agent A2A;
- a broad database migration.

