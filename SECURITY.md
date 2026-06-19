# Security Policy

tmuxbot controls local AI CLI processes through tmux and can run those CLIs with
high-permission flags. Treat deployments as sensitive automation surfaces.

## Supported Versions

Only the active branch is supported for security fixes before formal releases
are established. Current active branch: `productization-prep`.

## Reporting

Do not open a public issue for secrets, token exposure, ACL bypasses, or remote
command execution risks. Report privately to the repository owner.

## Sensitive Files

These files must not be committed:

- `.env`
- `bindings.yaml`
- `data/`
- `CLAUDE.md`
- local CLI credentials under `~/.claude`, `~/.codex`, or equivalent locations

## Security Boundaries

- Unknown IM sources must stay silent unless explicitly provisioned.
- ACL requires both user identity and configured source binding.
- Production should use explicit `CLAUDE_BIN` / `CODEX_BIN` paths instead of
  relying on shell `PATH`.
- Do not add hooks or automation that silently expands CLI permissions beyond
  documented startup flags.
