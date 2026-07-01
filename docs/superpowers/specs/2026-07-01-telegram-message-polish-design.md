# Telegram Message Polish Design

## Scope

Improve Telegram delivery for tmux-backed assistant output without adding SDK backends or Telegram draft streaming.

## Goals

- Preserve the existing work status aggregator: `工作中`, tool calls, plan updates, and completion edits.
- Send final assistant replies through a richer Telegram path when the frontend supports it.
- Keep a plain `send_html` fallback for Feishu and any frontend without Telegram-specific capabilities.
- Make long replies easier to consume: short readable message in chat, full content as an attachment.
- Expose session actions on final replies: screen, status, stop.

## Design

- Add a small reply rendering module that turns assistant HTML text plus binding metadata into a structured payload.
- Add an optional Telegram method for assistant replies. `jsonl.py` uses it when available, otherwise it keeps the current send path.
- The Telegram method sends readable HTML chunks, attaches full text for very long replies, and adds inline keyboard actions.
- Status/plan/tool messages remain separate from final replies.

## Non-Goals

- No SDK/API backend.
- No `sendMessageDraft` or `sendRichMessageDraft` path in this phase.
- No Feishu card rewrite in this phase.

## Tests

- Final replies use the enhanced frontend method when available.
- Plain frontends still receive the original final reply behavior.
- Long replies produce a summary message plus a full text attachment.
- Existing attachment, plan, tool aggregation, and live text dedupe tests remain valid.
