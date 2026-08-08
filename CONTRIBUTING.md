# Contributing

tmuxbot is a local TUI bridge for interactive AI CLIs. Changes should preserve
the core contract: IM frontends transport messages, tmux panes remain the
execution surface, and local CLI session logs remain the source of truth.

## Development Setup

```bash
make install-dev
make check
make check-web
```

`make check` runs Python compile checks, tests, and ruff. `make check-web`
installs the locked npm dependencies, runs Vitest, and builds the production WebUI.

## Change Expectations

- Keep behavior changes covered by focused tests.
- Update `README.md`, `DEVELOPMENT.md`, `.env.example`, or
  `bindings.example.yaml` when configuration or operator behavior changes.
- Update `CHANGELOG.md` under `Unreleased`.
- Keep sensitive files out of git: `.env`, `bindings.yaml`, `data/`,
  `CLAUDE.md`.
- Prefer shared frontend/backend abstractions over duplicating behavior in one
  adapter.

## Pull Requests

Every pull request should describe:

- what changed
- why it changed
- how it was tested
- any deployment or migration impact

For release changes, also follow `VERSIONING.md` and `RELEASE.md`.
