# Release Process

tmuxbot releases are operator-focused. A release should make clear what changed
for deployment, configuration, IM behavior, and CLI lifecycle behavior.

## Before Release

- `git status --short` is clean except ignored local runtime files.
- `make check` passes.
- `README.md`, `DEVELOPMENT.md`, `PRODUCTIZATION.md`, and `CHANGELOG.md` match
  the shipped behavior.
- `.env.example` and `bindings.example.yaml` include any new required config.
- Sensitive files remain ignored: `.env`, `bindings.yaml`, `data/`, `CLAUDE.md`.

## Release Notes Shape

Use these headings when they apply:

- `Added`
- `Changed`
- `Fixed`
- `Removed`
- `Security`
- `Docs`
- `Operations`

Operational notes should mention:

- systemd changes
- required environment variables
- binding schema changes
- Claude/Codex startup or lifecycle changes
- migration steps for local and hbhy deployments

## Post Release

- Verify GitHub tag and release page.
- Pull the target deployment branch on each host.
- Restart the relevant systemd user service.
- Check `journalctl --user -u tmuxbot -n 80 --no-pager`.
