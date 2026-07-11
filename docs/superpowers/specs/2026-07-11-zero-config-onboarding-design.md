# tmuxbot Zero-Configuration Onboarding and Reliable Input Design

Date: 2026-07-11 UTC

Status: Direction approved; written specification awaiting user review; implementation not started.

## Goal

Turn tmuxbot into a single-user local appliance with one installation command and one run command:

```bash
uv tool install 'tmuxbot[full]'
tmuxbot serve --open
```

The first launch must work without `.env`, `bindings.yaml`, channel credentials, or configured providers. Remaining setup moves into the WebUI.

## Delivery sequence

1. **Z0 Reliable input hotfix:** restore a configurable 0.5-second post-bracketed-paste settle window inside the serialized tmux input runtime before Enter.
2. **Z1 Zero-config bootstrap:** XDG paths, `full` extra, `serve --open`, empty-config startup, local setup secret generation, `doctor`, and a bridge supervisor.
3. **Z2 Provider and configuration wizard:** allowlisted Claude/Codex/tmux discovery, passive and active probes, verified session model switching, project/channel/binding configuration, SQLite authority, and a protected secret store.
4. **Z3 Web terminal:** xterm.js, PTY-backed tmux attach, observe mode, audited takeover, short-lived terminal tickets, and a generated systemd user service.
5. **Z4 TeamRun:** deterministic Coordinator/Implementer/Reviewer scheduling with DAGs, mailbox messages, artifacts, a shared-directory write lease, review, and acceptance gates.

## Experience milestones

- **Preview A:** after Z1 and the Web shell—open the dashboard, authenticate, view host/tmux health, and see discovered CLI candidates.
- **Preview B:** after Z2/Z3—configure providers and projects, run reply/model probes, and operate one tmux CLI through the Web terminal.
- **Preview C:** after Z4—run the first two-or-more-LLM collaboration loop with evidence and independent review.

## Provider probe contract

```text
detect_candidates
probe_binary
build_launch_argv
build_resume_argv
probe_reply
switch_model
verify_model
terminal_status
```

Discovery uses an allowlist, argv execution without a shell, a three-second timeout, bounded output, realpath/inode/version evidence, and explicit user authorization. Active reply probes may consume provider quota and therefore require a deliberate user action and a tmuxbot-owned test session.

Model switching is capability-based. Launch-time selection and live-session switching are separate. A switch is successful only after provider status or an equivalent native signal verifies the active model.

## Configuration authority

New installations use SQLite as the configuration authority for provider profiles, channels, projects, bindings, managed sessions, probe results, and configuration revisions. Secrets live in a separate mode-`0600` store; APIs return only configured state or masked values.

Legacy `.env` and `bindings.yaml` receive a one-time preview-and-confirm import. They must not remain a second writable authority.

## Terminal security

- Server-resolved session IDs only; browsers never submit raw tmux targets or commands.
- Fixed executable plus argv; no shell.
- Observe-only by default.
- Audited takeover blocks bridge/scheduler injection to the same target.
- Single-use, short-lived tickets bound to the authenticated Web session.
- Strict WebSocket Origin checks and bounded I/O.
- Browser disconnect never kills the tmux session.

## Acceptance highlights

- Empty HOME and no legacy files: `tmuxbot serve` remains healthy.
- The WebUI completes first setup and shows an unconfigured bridge instead of exiting.
- Claude and Codex share provider contract tests.
- Passive probes make no model call; active probes require explicit confirmation and evidence.
- Model switch failure never updates persisted success state.
- Z0 proves `inspect → paste → sleep(0.5) → Enter`; `with_enter=False` performs neither delay nor Enter.
- Z4 proves Coordinator → Implementer → Reviewer → accepted, with no acceptance from worker self-report alone.

The Chinese specification is the authoritative detailed version for implementation scope, security constraints, current completion status, and test criteria.
