# Cross-Channel Rich Messages Design

## Goal

Make assistant replies render consistently across Codex and Claude while using the native strengths of Telegram and Feishu. Tmux remains the runtime and control surface; this change only restructures outbound presentation and channel actions.

## Scope

- Introduce a channel-neutral reply document derived from the existing `ReplyEnvelope`.
- Render that document as Telegram HTML plus inline keyboard actions.
- Render it as a Feishu Card JSON 2.0 interactive card with header, summary, structured body, status note, and action buttons.
- Add Feishu card-action callback handling for screen, status, cancel, and interrupt.
- Preserve long-output files, attachments, message replacement, thread behavior, and legacy Feishu-card fallback.
- Add optional Feishu streaming behind a capability/configuration gate after the static card path is stable.

Out of scope: replacing tmux, changing provider event ingestion, introducing a web UI, or adopting Telegram business-only checklist features.

## Architecture

Provider-specific code continues to produce `ReplyEnvelope`. A new pure rendering layer converts the envelope and binding context into a small `ReplyDocument` model containing header, body blocks, status, attachments, and actions. Channel renderers consume this model directly:

```text
Codex / Claude events
        |
   ReplyEnvelope
        |
   ReplyDocument
      /       \
Telegram     Feishu
HTML/entity  Card JSON 2.0
```

No channel output becomes the input to another channel renderer. In particular, Feishu will no longer parse Telegram HTML with regular expressions.

## Reply Document

The first implementation keeps the model intentionally small:

- `ReplyDocument`: title, binding name, body blocks, optional status, actions, attachments, replacement key, and notification flag.
- Blocks: paragraph, heading, fenced code, list, quote, and divider.
- Actions: screen, status, cancel, and interrupt, using the existing canonical action names.

The parser accepts the Markdown-shaped provider body already used by `ReplyEnvelope`. Unknown syntax remains a paragraph instead of being dropped. Provider identity and model details come from structured metadata/status, never from assumptions about the last output line.

## Telegram Rendering

Telegram keeps the current proven interaction model:

- HTML for the initial implementation, with escaped text and balanced tags per split chunk.
- Inline keyboard for supported actions; interrupt retains the existing confirmation step.
- Expandable blockquotes for verbose diagnostics where supported.
- Link previews disabled by default for CLI output and enabled only through explicit metadata.
- Long replies use a bounded preview plus the existing UTF-8 text attachment.

The renderer interface will permit a later switch to explicit `MessageEntity` arrays without changing the reply document. `sendRichMessageDraft` remains experimental and is not part of the first production rollout.

## Feishu Rendering

The preferred renderer emits Card JSON 2.0:

- Header with title, provider tag, and state color.
- `config.summary.content` containing a safe single-line preview for the conversation list.
- Separate Markdown/code body components with stable `element_id` values.
- Footer/status as a note component rather than italic Markdown.
- Native buttons for screen, status, cancel, and interrupt.
- Collapsible detail area for verbose tool/diagnostic content when present.
- Serialized-size preflight. Cards approaching the 30 KB limit fall back to a summary card plus the full text file.

State colors are blue for working, green for completed/idle, orange for waiting, red for failed/interrupted, and gray when no state is known.

Card JSON 2.0 is gated by configuration/capability. The existing simple card remains the fallback for deployments that require clients older than Feishu 7.20.

## Feishu Actions

Button values carry a compact binding token and canonical action, matching Telegram semantics. The callback handler validates the token against configured bindings and dispatches through the existing command adapter. Interrupt is a two-step interaction: the first action returns a confirmation card, and only the confirmation sends Ctrl-C to tmux.

Callbacks are idempotent where practical. Invalid, expired, or unauthorized tokens receive a short error response and never reach tmux.

## Streaming and Updates

Static Card JSON 2.0 ships first. Feishu streaming is a separate gated phase:

- Create a card entity in streaming mode.
- Update only the body component at a throttled rate of at most five application updates per second.
- Require each streamed text update to extend the previous text prefix.
- Close streaming before enabling action callbacks on the final card.
- Fall back to normal whole-card PATCH when card entities or streaming are unavailable.

Telegram continues sending finalized messages initially. Its new rich-message draft API will be evaluated only after framework support is verified.

## Compatibility and Failure Handling

- Existing `ReplyEnvelope` producers remain source compatible.
- Existing text commands remain available even after Feishu buttons are enabled.
- A renderer failure falls back to plain text or the legacy simple card rather than dropping the reply.
- Unsupported blocks degrade to escaped text.
- Attachment upload failures do not invalidate the main reply.
- All callback payloads remain within platform limits and contain no filesystem paths or secrets.
- Tmux targets and command dispatch remain unchanged.

## Testing

- Pure parser tests for headings, code fences, lists, quotes, malformed Markdown, and provider-neutral endings.
- Golden-structure tests for Telegram output and Feishu Card JSON 2.0.
- Size-boundary tests for card/file fallback.
- Callback tests for every action, invalid tokens, authorization, and interrupt confirmation.
- Contract tests proving Codex and Claude envelopes produce equivalent channel semantics.
- Regression tests for attachments, threads, long output, edits, and legacy Feishu fallback.
- End-to-end matrix checks for Codex/Claude × Telegram/Feishu while retaining tmux as the runtime.

## Rollout

1. Add the neutral document/parser behind existing frontend behavior.
2. Switch Telegram to the neutral renderer without changing visible semantics.
3. Enable Feishu Card JSON 2.0 static cards and callbacks on the test deployment.
4. Verify callbacks, long-output fallback, Claude endings, and legacy card fallback.
5. Enable Card JSON 2.0 on the hbhy Feishu deployment.
6. Add and canary Feishu streaming separately.

Rollback is configuration-only: disable Card JSON 2.0/streaming and use the legacy Feishu card path. Telegram can independently retain its current HTML renderer during rollout.

## Success Criteria

- Codex and Claude final replies render without relying on provider-specific trailing lines.
- Telegram retains all current buttons, attachments, threads, and long-output behavior.
- Feishu shows structured Card JSON 2.0 content and working native action buttons.
- Interrupt requires confirmation on both channels.
- Oversized output is never silently truncated or rejected.
- The full automated test suite passes, followed by live Telegram and Feishu acceptance tests against tmux-backed sessions.
