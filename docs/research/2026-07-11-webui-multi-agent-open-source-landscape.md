# WebUI Multi-Agent Open-Source Landscape

Date: 2026-07-11 UTC

Status: Research only. No third-party code reuse or WebUI implementation is authorized by this document.

## Conclusion

The native tmuxbot WebUI design is feasible and is supported by several independent open-source patterns. No candidate combines tmuxbot's existing Telegram/Feishu channels, attachments, native provider-session resume, Runtime V2, and normalized event pipeline. The recommended strategy is selective pattern adoption, not replacement or whole-project embedding.

## Best references

| Project | Pattern to study | License note |
|---|---|---|
| [Guppi](https://github.com/ekristen/guppi) | PTY-backed xterm.js, tmux control mode, alerts | MIT |
| [agent-dashboard](https://github.com/bjornjee/agent-dashboard) | Provider adapters, hooks/transcripts, PWA, workflow gates | MIT |
| [Parallel Code](https://github.com/johannesjo/parallel-code) | Task worktrees, diff review, mobile access, service ports | MIT |
| [ruah-orch](https://github.com/ruah-dev/ruah-orch) | DAG, file claims, artifacts, takeover and resume | MIT |
| [Agent Deck](https://github.com/asheshgoplani/agent-deck) | Fork/resume/archive, worktree lifecycle, conductor notifications | MIT |
| [Session Deck](https://github.com/JesseProjects-LLC/session-deck) | Authentication, SQLite, persistent terminal layouts | MIT |

TermHive and webmux have no detected license and are product-idea references only. Claude Squad and Claude Codex Bridge are AGPL and should not contribute copied code. NTM carries a custom OpenAI/Anthropic exclusion rider and is excluded from implementation analysis and reuse.

## Recommended additions to the approved spec

1. Session-scoped Coordinator MCP configuration with global-config protection.
2. `.worktreeinclude`, bounded setup/teardown hooks, service-port allocation, and cleanup.
3. Pre-dispatch file claims plus post-task actual-diff validation.
4. Orphan session adopt/archive/ignore recovery.
5. Separate desktop Command Center and simplified mobile interaction surfaces.
6. Explicit terminal observe/control modes with takeover auditing.
7. RunEvent as the single state input for WebUI, Telegram, Feishu, and notifications.
8. Strict separation of durable project knowledge, TeamRun artifacts, and provider-private transcripts.

## Patterns to avoid

- Unauthenticated web terminals.
- Unrecorded pane-to-pane agent messages.
- Overwriting user instruction or global MCP files.
- Shared writable wiki files updated concurrently by agents.
- Auto-merge based only on an agent's completion claim.
- Copying every ignored file into worktrees.
- Keeping scheduler truth only in a conductor LLM context.
- Broadcasting complete context to every agent.

