# Single-service production operations

A production host should run one tmuxbot systemd user service. The service owns
one Web/supervisor process and one bridge child; that bridge can host every
configured Telegram Bot and Feishu App credential.

## Availability layers

| Layer | Mechanism | Recovery behavior |
|---|---|---|
| Boot/logout survival | `systemctl --user enable` + user linger | starts without an interactive login |
| Main process | `Restart=always`, `RestartSec=5`, `StartLimitIntervalSec=0` | restarts after crashes without a permanent systemd start-limit |
| Bridge child | `BridgeSupervisor` | respawns the bridge with bounded backoff |
| Telegram/Feishu transport | frontend reconnect loop | reconnects a failed polling/WebSocket connection |
| tmux/provider TUI | `TMUXBOT_LIFECYCLE_ENABLED=1` | recreates missing targets and resumes persisted Claude/Codex/Pi identities; an explicitly missing Claude resume ID falls back once to a fresh TUI |

The lifecycle watchdog never runs `tmux kill-server`. Existing panes survive
service deploys because the unit uses `KillMode=process`.

## Install

```bash
uv tool install 'tmuxbot[full]'
tmuxbot install-service --now --self-heal
loginctl enable-linger "$USER"

systemctl --user status tmuxbot.service
journalctl --user -u tmuxbot.service -n 100 --no-pager
```

`install-service` writes only `~/.config/systemd/user/tmuxbot.service`. It also
disables and removes the legacy `tmuxbot-bridge-refresh@tmuxbot.timer`; periodic
forced restarts are not part of the availability model.

## Multiple credentials

Keep all non-overlapping routes in one bindings file. `bot_token_env` identifies
the Telegram Bot or Feishu App credential; `backend` remains a route property.
The bridge groups routes by `(channel, bot_token_env)` and starts one frontend
per credential.

`lark-oapi` 1.x stores its WebSocket loop in a module global. tmuxbot loads one
private WebSocket module and worker loop per Feishu credential, so multiple apps
can safely run in the same bridge process.

Do not create a second service merely for another credential. A deliberate
multi-instance deployment must use completely separate bindings, route keys,
offsets, state/data directories, locks, and databases.

## Consolidating an older multi-service host

Use a maintenance window and keep a rollback copy.

1. Inventory every enabled tmuxbot unit, timer, bindings file, data/state path,
   route endpoint, tmux target, and process.
2. Build a merged bindings candidate and run `tmuxbot route validate` before
   touching services.
3. Merge offsets by transcript path using the greatest byte offset. Never reset
   an offset backwards: doing so can replay historical assistant output to IM.
   Keep the state directory `0700` and offsets file `0600`.
4. Compare control-plane databases. Migrate non-empty records explicitly; do not
   silently discard one instance's database.
5. Record tmux pane PID/cwd/current command so the cutover can prove provider
   TUIs were preserved.
6. Stop every old bridge, atomically install the merged bindings/offsets, then
   start only `tmuxbot.service`.
7. Verify one service process, one bridge child, every expected frontend,
   channel-health state, lifecycle watchdog start, and unchanged tmux panes.
8. Run a real inbound/outbound smoke for every credential and at least one exact
   topic/thread route.
9. Only after acceptance, disable/remove old app-specific units and rotation or
   bridge-refresh timers. Retain data backups until the next normal operating
   window.

## Verification and failure recovery

```bash
systemctl --user is-enabled tmuxbot.service
systemctl --user show tmuxbot.service -p ActiveState -p SubState -p NRestarts
systemctl --user list-units --type=service --all | grep tmuxbot
systemctl --user list-timers --all | grep tmuxbot
journalctl --user -u tmuxbot.service -n 200 --no-pager
```

Expected log markers include:

```text
lifecycle watchdog starting
feishu ws starting                 # once per configured Feishu credential
N frontend(s) ready
```

To test process recovery, terminate only the systemd main PID and confirm the
unit returns to `active/running` with an incremented `NRestarts`. To test session
recovery, close one disposable bound tmux session and confirm the watchdog
recreates the full target and resumes its persisted provider identity. Do not
perform either test on a pane running an irreplaceable active task.

If consolidation fails, stop the new service, restore the backed-up bindings,
offsets, unit files/drop-ins, and data paths, run `systemctl --user daemon-reload`,
and restart the former services. Never use `tmux kill-server` during rollback.
