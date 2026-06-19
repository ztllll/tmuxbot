# Support

tmuxbot is maintained as an operator tool. Good support requests include enough
local state to reproduce the issue without exposing secrets.

## Before Opening an Issue

Run:

```bash
make check
bash bin/status.sh
journalctl --user -u tmuxbot -n 80 --no-pager
```

For tmux pane issues, also capture:

```bash
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{pane_current_command} #{pane_current_path}'
tmux capture-pane -t <session>:0.0 -p -S -80
```

Redact tokens, chat IDs, open IDs, local credentials, private paths, and project
contents before sharing logs.

## Useful Context

- frontend: Telegram or Feishu
- backend: Claude Code or Codex
- deployment: local script or systemd user service
- Python version
- tmux version
- CLI path from `CLAUDE_BIN` / `CODEX_BIN`
- whether the pane is at a shell, a picker, or a ready TUI prompt
