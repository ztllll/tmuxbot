# Codex Watchdog Process Detection Design

## Problem

The lifecycle watchdog checks every Codex binding periodically. The backend currently treats only
`node` as a running Codex process. Standalone Codex releases expose `codex` as the tmux pane's
current command, so a healthy TUI is misclassified as stopped. The watchdog then pastes
`codex --dangerously-bypass-approvals-and-sandbox` into that live TUI as a user prompt.

## Design

Codex process detection accepts both supported launch shapes:

- npm wrapper: `node`
- standalone binary: `codex`

Startup injection is permitted only when the pane is running a known interactive shell:
`bash`, `zsh`, `sh`, or `fish`. An unexpected foreground process is left untouched and logged.
The readiness poll uses the same Codex process predicate so both launch shapes can become ready.

## Tests

Regression tests exercise `CodexBackend.ensure_running` with controlled tmux helpers:

- `node` and `codex` return without injecting a command.
- `bash` injects the configured Codex start command once.
- an unknown command does not receive injected text.

After deployment, the service log and pane history must remain free of watchdog-generated startup
prompts for at least two lifecycle intervals.

