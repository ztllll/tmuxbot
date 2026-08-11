# tmuxbot — Productization Status

This document records the product boundary, the state frozen for the 0.3.0
release, and the next development seams. Historical implementation details live
in Git and `CHANGELOG.md`.

## Product Boundary

tmuxbot is a local control plane for interactive AI CLIs. Telegram, Feishu, and
the WebUI transport or present commands; tmux panes remain the execution surface.
Claude Code, Codex, and Pi session logs, provider events, and terminal state
remain the local sources of truth.

The project deliberately does not replace the interactive CLI with a vendor API,
SDK, or headless `claude -p` process.

## 0.3.0 Frozen Baseline

The 0.3.0 line is feature-frozen with these shipped capabilities:

- Telegram and Feishu frontends for Claude Code and Codex bindings.
- Shared dispatch, command adapters, interaction controls, attachment handling,
  rich replies, task/plan projection, and provider status footers.
- Safe serialized tmux input with idle detection, composer verification, bounded
  Enter retries, transcript identity checks, and JSONL backlog protection.
- Runtime V2 provider/channel contracts with `off`, `shadow`, and `on` rollout
  modes, plus optional local Claude hook spooling.
- Message-driven lazy tmux lifecycle by default, provider session recovery, and
  explicit Web/IM stop controls that preserve binding history.
- XDG runtime paths, configuration validation, `tmuxbot doctor`, single systemd
  user service installation, supervised WebUI + bridge startup, and opt-in
  tmux/provider session self-healing.
- Authenticated Chinese WebUI with first-run setup, provider discovery, project
  and managed-session records, channel setup, read-only tmux inventory, and
  audited xterm.js takeover.
- Deterministic TeamRun foundations: role contracts, DAG scheduling, single-writer
  leases, mailbox/artifact records, isolated worktrees, reviewer gates, recovery,
  and Web/API projections.
- Python package metadata, wheel checks, CI, release documentation, and broad unit
  and tmux-backed integration coverage.

## Current Main After 0.3.0

The `v0.3.0` tag remains the frozen release point. Current `main` is an Unreleased
maintenance line on top of that baseline and additionally includes:

- exact Telegram topic and Feishu thread routes down to unique tmux panes, with
  route-local Claude Code, Codex, or Pi adapter selection and deterministic Admin
  discover/plan/apply/verify transactions;
- Pi-native session handoff, command classification, statusline/usage projection,
  persistent current-branch Todo panels, Working-state steering input, and JSONL
  hard-signal confirmation for `/new`, `/clone`, `/resume`, and compaction;
- non-destructive hourly route health audit and fail-closed cold wake: stale TUI
  scrollback cannot block a shell launch, while manually closed sessions remain
  dormant and provider TUIs are never idle-exited;
- editable Pi auto-compaction lifecycle cards with session-history ETA, completion
  metadata, provider-error visibility, and explicit warning when compaction exits
  without a JSONL completion marker;
- Feishu long-reply pagination constrained by both the 30KB Card JSON payload and
  a maximum of 50 body elements per card, preserving exact thread routing;
- Telegram final-reply delivery confirmation: every chunk must return a Bot API
  message ID before the transcript offset advances, so transient send failure is
  retried instead of silently losing the assistant response;
- source-checkout service restarts reinstall the active uv tool before restarting
  systemd, preventing deployed site-packages from lagging behind `main`.

The authoritative itemized record for this line is the `Unreleased` section of
`CHANGELOG.md`; release version metadata remains `0.3.0` until the next release is
cut.

## Architectural Invariants

- tmux is the only provider execution plane.
- One IM bot/application identity may serve multiple exact topic routes and adapters.
- `(channel, bot_token_env, chat_id, thread_id)` and full tmux targets are unique.
- A frontend routes only to bindings assigned to that frontend.
- Unknown sources are silent; route provisioning occurs through explicit configuration, route CLI, Admin DM, or the authenticated Web control plane rather than an unbound IM message.
- Admin LLM correctness does not depend on provider-specific memory: a managed Operations Contract plus `tmuxbot admin` discover/plan/apply/verify transactions owns Telegram link parsing, topic discovery, full-candidate validation, atomic YAML replacement, supervised restart, post-apply verification, and rollback.
- Normal text injection never sends Escape first.
- Provider parsing and launch policy belong behind backend/provider seams, not in
  transport-specific handlers.
- Semantic interaction buttons are conveniences over the active tmux pane; they
  are not a second state machine or source of truth.
- Credentials and runtime data remain local and ignored by Git.
- A production host runs one tmuxbot systemd service; multiple channel
  credentials are isolated inside that bridge rather than by duplicate units.

## Known Maintenance Debt

These are not release blockers for 0.3.0, but they are the first candidates for
future work:

1. `TelegramFrontend`, `FeishuFrontend`, `web/app.py`, and the control-plane
   repository are large modules with mixed orchestration concerns.
2. Interaction state is still partly reconstructed from terminal captures rather
   than persisted as a complete per-binding transaction model.
3. Lifecycle readiness and recovery are observable but not yet represented by one
   explicit provider-neutral state machine.
4. The Web bundle should be split before substantially expanding the console.
5. Starlette/httpx and lark-oapi currently emit upstream deprecation warnings in
   tests and should be revisited during dependency maintenance.
6. Production validation still requires real Telegram/Feishu endpoint smoke tests;
   CI intentionally uses local contracts and temporary tmux panes instead of live
   credentials. The Admin transaction layer verifies route/tmux/service state, while
   final IM delivery and thread anchoring remain explicit live acceptance steps.
7. Transcript offset retry now prevents silent Telegram loss, but it is not yet a
   durable cross-channel outbox with idempotency keys; a process crash after remote
   send and before offset persistence can still produce an at-least-once duplicate.

## Next Development Gate

The accepted topic-routing/Admin-DM design is documented in
[`docs/topic-routing.md`](docs/topic-routing.md) and ADR-0001.

Before adding another major frontend, backend, or TeamRun workflow:

1. Choose one deep-module seam rather than adding more routes or handlers directly
   to the existing large files.
2. Write the behavior/spec document and regression tests first.
3. Preserve the 0.3.0 execution and security invariants above.
4. Run Python checks, WebUI tests/build, `tmuxbot doctor`, and the documented live
   channel smoke matrix before release.

The next version starts from `main`; short-lived feature branches should be merged
back promptly and removed after verification.
