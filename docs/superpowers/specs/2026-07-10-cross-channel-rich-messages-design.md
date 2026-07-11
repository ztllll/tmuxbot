# Cross-Channel Rich Messages Design

## Goal

Make assistant replies render consistently across Codex and Claude while using the native strengths of Telegram and Feishu. Tmux remains the runtime and control surface; this change only restructures outbound presentation and channel actions.

## Scope

- Introduce a channel-neutral reply document derived from the existing `ReplyEnvelope`.
- Render that document as clean Telegram HTML without persistent action buttons.
- Render it as a Feishu Card JSON 2.0 card with header, summary, structured body, status color, and compact metadata.
- Use slash commands as the sole operation entry point for new Telegram and Feishu messages.
- Preserve long-output files, attachments, message replacement, thread behavior, and legacy Feishu-card fallback.
- Promote referenced local documents and images to real channel attachments instead of exposing local filesystem paths.
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

- `ReplyDocument`: title, binding name, body blocks, optional display status, attachments, replacement key, notification flag, and legacy action metadata.
- Blocks: paragraph, heading, fenced code, list, quote, and divider.
- Legacy actions retain canonical names for old-message callback compatibility, but new renderers do not emit controls from them.

The parser accepts the Markdown-shaped provider body already used by `ReplyEnvelope`. Unknown syntax remains a paragraph instead of being dropped. Provider identity and model details come from structured metadata/status, never from assumptions about the last output line.

## Local Attachment Promotion

Attachment delivery uses a hybrid contract:

1. Structured `ReplyEnvelope.attachments` is authoritative whenever the provider/runtime supplies it.
2. A deterministic fallback scanner extracts explicit local-file references from the body before rendering.

The fallback scanner recognizes existing regular files referenced as:

- Markdown links and images, including angle-bracket targets and optional `:line` or `#Lline` suffixes.
- `file://`, `@/absolute/path`, absolute paths, and `./relative/path` references.
- Standalone path lines, tmux-guttered lines, and paths following labels such as `文件:` or `图片:`.
- Inline Markdown links while preserving the surrounding sentence.

Relative paths resolve against the binding working directory. A reference is promoted only when it resolves to an existing regular file under an allowed root. Default allowed roots are the binding working directory, the tmuxbot attachment directory, and the operating-system temporary directory; deployments may add roots explicitly. Directory references, devices, sockets, missing files, and paths outside allowed roots remain text and are never uploaded.

Images use the channel image API; all other permitted MIME types use the channel file/document API. Duplicate references resolve to one upload per reply. Promoted path syntax is removed from the rendered body and replaced with a short filename label only when removal would otherwise make the sentence unreadable.

Upload behavior is channel-neutral:

- Telegram uploads images with `send_photo` and documents with `send_document`.
- Feishu uploads images to obtain an `image_key` and files to obtain a `file_key`, then sends the corresponding resource message.
- Captions use the safe basename and optional surrounding link text, never the absolute local path.
- An upload failure produces a visible `附件发送失败: <basename>` notice and retains enough server-side logging to diagnose the channel API response; it does not reveal the local path to the chat.
- File size and MIME checks run before upload. Unsupported or oversized files receive the same basename-only failure notice.

This scanner is a compatibility fallback, not a substitute for structured attachments. Provider adapters should attach generated artifacts explicitly when their event formats expose them.

## Telegram Rendering

Telegram keeps the current proven interaction model:

- HTML for the initial implementation, with escaped text and balanced tags per split chunk.
- A compact text-native state badge between header and body: yellow-circle working, orange-circle waiting, check-mark completed/idle, red-circle error/blocked/dead, blue-circle information, and white-circle unknown. Telegram has no Feishu-equivalent API for selecting a colored card header or message background.
- No persistent inline keyboard; users operate tmux through `/screen`, `/status`, `/esc`, `/cc`, and the existing command set.
- Expandable blockquotes for verbose diagnostics where supported.
- Link previews disabled by default for CLI output and enabled only through explicit metadata.
- Long replies use a bounded preview plus the existing UTF-8 text attachment.

The renderer interface will permit a later switch to explicit `MessageEntity` arrays without changing the reply document. `sendRichMessageDraft` remains experimental and is not part of the first production rollout.

## Feishu Rendering

The preferred renderer emits Card JSON 2.0:

- Header with title, provider tag, and state color.
- `config.summary.content` containing a safe single-line preview for the conversation list.
- Separate Markdown/code body components with stable `element_id` values.
- Footer/status as grey notation-sized text rather than the Card JSON 2.0-deprecated note component.
- No persistent buttons or overflow menus; slash commands keep the card visually clean.
- Collapsible detail area for verbose tool/diagnostic content when present.
- Serialized-size preflight. Cards approaching the 30 KB limit fall back to a summary card plus the full text file.

State colors are yellow for working, orange for waiting, green for completed/idle, red for blocked/dead/provider errors, blue for informational cards, and grey when no state is known. Streaming cards begin yellow and close green after a successful final reply.

Card JSON 2.0 is gated by configuration/capability. The existing simple card remains the fallback for deployments that require clients older than Feishu 7.20.

## Command-Only Actions

New Telegram and Feishu replies do not emit action buttons. Existing slash commands remain the canonical interface and continue dispatching through the shared command adapter into tmux. Channel capabilities advertise `supports_actions=False` so future renderers do not reintroduce persistent controls implicitly.

Previously sent Telegram keyboards and Feishu cards may still be clicked. Their callback handlers remain temporarily available for backward compatibility and retain binding-token, ACL, chat-correlation, and Ctrl-C confirmation checks.

## Streaming and Updates

Static Card JSON 2.0 ships first. Feishu streaming is a separate gated phase:

- Create a card entity in streaming mode.
- Update only the body component at a throttled rate of at most five application updates per second.
- Require each streamed text update to extend the previous text prefix.
- Close streaming by replacing the yellow working card with a green final card.
- Fall back to normal whole-card PATCH when card entities or streaming are unavailable.

Telegram continues sending finalized messages initially. Its new rich-message draft API will be evaluated only after framework support is verified.

## Compatibility and Failure Handling

- Existing `ReplyEnvelope` producers remain source compatible.
- Existing text commands remain the only operation entry point on new cards.
- A renderer failure falls back to plain text or the legacy simple card rather than dropping the reply.
- Unsupported blocks degrade to escaped text.
- Attachment upload failures do not invalidate the main reply.
- Local paths are never rendered into Telegram or Feishu when they identify an uploadable attachment.
- Automatic attachment promotion is restricted to allowed roots and existing regular files.
- All callback payloads remain within platform limits and contain no filesystem paths or secrets.
- Tmux targets and command dispatch remain unchanged.

## Testing

- Pure parser tests for headings, code fences, lists, quotes, malformed Markdown, and provider-neutral endings.
- Golden-structure tests for Telegram output and Feishu Card JSON 2.0.
- Size-boundary tests for card/file fallback.
- Local attachment tests covering absolute paths, relative paths, Markdown image/link syntax, line-number suffixes, inline links, duplicate references, allowed-root rejection, missing files, and upload failures.
- Cross-channel tests proving promoted images/files call the native Telegram and Feishu attachment APIs and never expose absolute paths in message text or captions.
- Regression tests proving new Telegram and Feishu replies contain no buttons while legacy callbacks remain safe.
- State-color tests for working, waiting, completed/idle, error, informational, and unknown cards.
- Contract tests proving Codex and Claude envelopes produce equivalent channel semantics.
- Regression tests for attachments, threads, long output, edits, and legacy Feishu fallback.
- End-to-end matrix checks for Codex/Claude × Telegram/Feishu while retaining tmux as the runtime.

## Rollout

1. Add the neutral document/parser behind existing frontend behavior.
2. Switch Telegram to the neutral renderer without changing visible semantics.
3. Enable button-free Feishu Card JSON 2.0 cards on the test deployment.
4. Verify slash commands, state colors, long-output fallback, Claude endings, and legacy card fallback.
5. Enable Card JSON 2.0 on the hbhy Feishu deployment.
6. Add and canary Feishu streaming separately.

Rollback is configuration-only: disable Card JSON 2.0/streaming and use the legacy Feishu card path. Telegram can independently retain its current HTML renderer during rollout.

## Success Criteria

- Codex and Claude final replies render without relying on provider-specific trailing lines.
- Telegram retains attachments, threads, long-output behavior, and slash commands without persistent buttons.
- Feishu shows structured Card JSON 2.0 content, state-aware header colors, and no persistent buttons.
- New messages use `/cc` for explicit interrupt; legacy button-triggered interrupt still requires confirmation.
- Oversized output is never silently truncated or rejected.
- Existing referenced local documents and images are sent as native attachments on both channels, with no absolute local path exposed to recipients.
- The full automated test suite passes, followed by live Telegram and Feishu acceptance tests against tmux-backed sessions.
