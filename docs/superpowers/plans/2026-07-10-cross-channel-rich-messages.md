# Cross-Channel Rich Messages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver provider-neutral rich replies, native Feishu Card JSON 2.0 actions, safer Telegram formatting controls, and automatic native delivery of referenced local documents/images.

**Architecture:** Keep `ReplyEnvelope` as the provider contract, normalize body/attachments once into a channel-neutral `ReplyDocument`, and render it independently for Telegram and Feishu. Explicit attachments remain authoritative while a cwd-aware, allowed-root scanner promotes local-file references as a compatibility fallback.

**Tech Stack:** Python 3.10+, asyncio, aiogram 3, lark-oapi 1.7+, pytest, tmux.

## Global Constraints

- Tmux remains the runtime and command-control surface.
- Codex and Claude must use the same reply contract without provider-tail heuristics.
- Never expose an uploadable absolute local path in channel text or captions.
- Automatic attachment roots default to binding cwd, tmuxbot attachment storage, and OS temp storage.
- Feishu Card JSON 2.0 must fall back to the legacy interactive card when unavailable.
- Interrupt actions require confirmation on Telegram and Feishu.
- Existing text commands, attachments, edits, threads, and long-output behavior remain compatible.

---

### Task 1: Cwd-aware local attachment promotion

**Files:**
- Modify: `tmuxbot/attachments.py`
- Modify: `tmuxbot/jsonl.py`
- Modify: `tmuxbot/frontends/telegram.py`
- Modify: `tmuxbot/frontends/feishu.py`
- Test: `tests/test_attachments.py`
- Test: `tests/test_outbound_attachments.py`
- Test: `tests/test_send_pre_attachments.py`

**Interfaces:**
- Produces: `split_outbound_attachments(text, *, cwd=None, allowed_roots=()) -> tuple[str, list[OutboundAttachment]]`.
- Produces: `OutboundAttachment(path, kind, label=None)` with safe basename-based captions.
- Consumes: `Binding.cwd`, `ATTACHMENT_DIR`, and explicit `ReplyEnvelope.attachments`.

- [ ] **Step 1: Write failing parser tests** for inline Markdown links/images, `:line` and `#Lline` suffixes, relative paths resolved from cwd, duplicate references, allowed-root rejection, and basename-only cleaned text.
- [ ] **Step 2: Run `pytest tests/test_attachments.py -q`** and verify the new tests fail because the current scanner only accepts standalone absolute path lines.
- [ ] **Step 3: Implement deterministic extraction** using explicit Markdown/file-reference patterns, regular-file checks, canonical allowed-root containment, suffix stripping, and ordered deduplication.
- [ ] **Step 4: Run `pytest tests/test_attachments.py -q`** and verify all attachment parser tests pass.
- [ ] **Step 5: Write failing integration tests** proving assistant replies and `send_pre` pass `Binding.cwd`, send images/files natively, and do not retain absolute paths in message text/captions.
- [ ] **Step 6: Update call sites** in `jsonl.py` and both frontends to provide cwd/roots and merge scanner results with explicit envelope attachments.
- [ ] **Step 7: Run `pytest tests/test_outbound_attachments.py tests/test_send_pre_attachments.py tests/test_channel_reply_contract.py -q`** and verify pass.
- [ ] **Step 8: Commit** with `git commit -m "feat: promote local paths to channel attachments"`.

### Task 2: Channel-neutral reply document and renderers

**Files:**
- Create: `tmuxbot/core/rich_messages.py`
- Modify: `tmuxbot/replies.py`
- Modify: `tmuxbot/core/__init__.py`
- Test: `tests/test_rich_messages.py`
- Test: `tests/test_telegram_replies.py`

**Interfaces:**
- Produces: `ReplyBlock`, `ReplyDocument`, `build_reply_document(binding, envelope, footer_text)`.
- Produces: `render_telegram_document(document, full_output_threshold) -> AssistantReply`.
- Produces: `reply_summary(document, limit=120) -> str`.
- Consumes: existing `ReplyEnvelope` and `TerminalStatus`.

- [ ] **Step 1: Write failing model/parser tests** for headings, fenced code with language, lists, quotes, paragraphs, unknown syntax fallback, Claude/Codex-neutral endings, and summary generation.
- [ ] **Step 2: Run `pytest tests/test_rich_messages.py -q`** and verify failure because the model does not exist.
- [ ] **Step 3: Implement immutable document/block dataclasses and a small line parser** that never infers provider identity from trailing text.
- [ ] **Step 4: Implement Telegram HTML rendering** with escaped prose, known inline formatting preservation, balanced block tags, expandable quotes, and existing long-output attachment semantics.
- [ ] **Step 5: Make `render_assistant_reply` delegate to the new document renderer** while preserving its public signature.
- [ ] **Step 6: Run `pytest tests/test_rich_messages.py tests/test_telegram_replies.py -q`** and verify pass.
- [ ] **Step 7: Commit** with `git commit -m "refactor: add channel-neutral reply documents"`.

### Task 3: Telegram delivery controls

**Files:**
- Modify: `tmuxbot/frontends/telegram.py`
- Test: `tests/test_telegram_replies.py`

**Interfaces:**
- Consumes: `ReplyEnvelope.metadata["link_preview"]` as an explicit boolean opt-in.
- Produces: aiogram `LinkPreviewOptions(is_disabled=True)` by default.

- [ ] **Step 1: Write failing tests** showing CLI replies disable link previews by default, explicit metadata enables them, buttons remain on only the first split chunk, and local attachment captions contain no absolute paths.
- [ ] **Step 2: Run `pytest tests/test_telegram_replies.py -q`** and verify the link-preview assertions fail.
- [ ] **Step 3: Implement `LinkPreviewOptions` selection** without changing current inline keyboard, confirmation, thread, or document fallback behavior.
- [ ] **Step 4: Run `pytest tests/test_telegram_replies.py -q`** and verify pass.
- [ ] **Step 5: Commit** with `git commit -m "feat: harden Telegram rich reply delivery"`.

### Task 4: Feishu Card JSON 2.0 rendering and fallback

**Files:**
- Create: `tmuxbot/frontends/feishu_cards.py`
- Modify: `tmuxbot/frontends/feishu.py`
- Modify: `tmuxbot/core/capabilities.py`
- Test: `tests/test_feishu_cards.py`
- Test: `tests/test_feishu_replies.py`
- Test: `tests/test_channel_reply_contract.py`

**Interfaces:**
- Produces: `build_feishu_card_v2(document, binding_token, *, confirm_interrupt=False) -> dict`.
- Produces: `serialize_feishu_card(card, max_bytes=30_000) -> str` with explicit oversize result/fallback handling.
- Modifies: `_send_card_sync(chat_id, content, *, legacy_fallback=True)` to accept serialized card JSON.

- [ ] **Step 1: Write failing card structure tests** for schema 2.0, summary, header/status color, stable element IDs, Markdown/code components, note footer, buttons, and confirmation-card shape.
- [ ] **Step 2: Run `pytest tests/test_feishu_cards.py -q`** and verify failure because the builder does not exist.
- [ ] **Step 3: Implement the pure Card JSON 2.0 builder** with legacy-card conversion and serialized byte-size preflight.
- [ ] **Step 4: Run `pytest tests/test_feishu_cards.py -q`** and verify pass.
- [ ] **Step 5: Write failing frontend tests** proving assistant replies send V2 content, report `supports_actions=True`, keep file/image attachments, and fall back to the legacy card after a V2 API failure.
- [ ] **Step 6: Integrate the builder into Feishu send/edit/reply paths** while retaining the legacy `_build_card` path for fallback and old-client configuration.
- [ ] **Step 7: Run `pytest tests/test_feishu_replies.py tests/test_channel_reply_contract.py -q`** and verify pass.
- [ ] **Step 8: Commit** with `git commit -m "feat: render Feishu Card JSON 2.0 replies"`.

### Task 5: Feishu native card actions

**Files:**
- Modify: `tmuxbot/frontends/feishu.py`
- Test: `tests/test_feishu_actions.py`

**Interfaces:**
- Produces: `_on_card_action(event) -> P2CardActionTriggerResponse` callback bridge.
- Consumes: card values `{token, action}` and existing `binding_by_token`, `handle_tui_action`, status summary, and interrupt confirmation behavior.

- [ ] **Step 1: Write failing callback tests** for ACL, binding lookup, refresh/status/cancel, interrupt confirmation, confirmed Ctrl-C, malformed values, and toast responses.
- [ ] **Step 2: Run `pytest tests/test_feishu_actions.py -q`** and verify failure because card callbacks are not registered or handled.
- [ ] **Step 3: Implement a thread-safe callback bridge** that validates `operator.open_id`, token, binding/chat correlation, schedules async work on `_main_loop`, and returns immediate success/error toasts.
- [ ] **Step 4: Register `register_p2_card_action_trigger` defensively** alongside message events and implement a confirmation replacement card for interrupt.
- [ ] **Step 5: Run `pytest tests/test_feishu_actions.py -q`** and verify pass.
- [ ] **Step 6: Commit** with `git commit -m "feat: handle Feishu card actions"`.

### Task 6: Gated Feishu streaming cards

**Files:**
- Create: `tmuxbot/frontends/feishu_streaming.py`
- Modify: `tmuxbot/frontends/feishu.py`
- Test: `tests/test_feishu_streaming.py`

**Interfaces:**
- Produces: `FeishuStreamingSession` with `create()`, `append(text)`, and `close(final_card)` methods.
- Consumes: lark-oapi CardKit v1 card/card-element APIs and Card JSON 2.0 stable element IDs.
- Falls back: normal interactive-card send/PATCH when creation or update fails.

- [ ] **Step 1: Write failing unit tests** for streaming card creation, prefix-only appends, five-updates-per-second throttling, component patching, close-before-actions, and API-failure fallback.
- [ ] **Step 2: Run `pytest tests/test_feishu_streaming.py -q`** and verify failure because the streaming session does not exist.
- [ ] **Step 3: Implement the isolated CardKit adapter** with injected clock/sleep/client callables so throttling and failures are deterministic in tests.
- [ ] **Step 4: Integrate streaming only when `TMUXBOT_FEISHU_STREAMING=1`** and retain the existing reply-stream edit path otherwise.
- [ ] **Step 5: Run `pytest tests/test_feishu_streaming.py tests/test_feishu_replies.py -q`** and verify pass.
- [ ] **Step 6: Commit** with `git commit -m "feat: add gated Feishu card streaming"`.

### Task 7: Cross-provider/channel regression and rollout controls

**Files:**
- Modify: `tmuxbot/config.py`
- Modify: `README.md`
- Modify: `tests/e2e/test_tmux_provider_channel_matrix.py`
- Modify: `tests/test_validation.py`

**Interfaces:**
- Consumes environment flags `TMUXBOT_FEISHU_CARD_V2` (default `1`) and `TMUXBOT_FEISHU_STREAMING` (default `0`).
- Preserves the Codex/Claude × Telegram/Feishu tmux runtime matrix.

- [ ] **Step 1: Write failing configuration/matrix tests** for V2 default, legacy opt-out, streaming default-off, provider-neutral local attachment semantics, and both channel capabilities.
- [ ] **Step 2: Run the targeted tests** and verify failures for missing rollout flags/matrix coverage.
- [ ] **Step 3: Add configuration plumbing and operational documentation** including Feishu callback subscription/permissions, Card 2.0 client requirement, allowed attachment roots, and rollback flags.
- [ ] **Step 4: Run `pytest tests/test_validation.py tests/e2e/test_tmux_provider_channel_matrix.py -q`** and verify pass.
- [ ] **Step 5: Run `.venv/bin/ruff check tmuxbot tests`** and fix all reported errors.
- [ ] **Step 6: Run `.venv/bin/pytest -q`** and verify the complete suite passes.
- [ ] **Step 7: Commit** with `git commit -m "docs: document rich message rollout controls"`.

### Task 8: Deployment acceptance

**Files:**
- No source changes unless a verified deployment-only defect is found through a new failing test.

**Interfaces:**
- Produces: verified Telegram and Feishu runtime behavior against tmux-backed Codex/Claude sessions.

- [ ] **Step 1: Inspect deployment scripts/service definitions** and identify the established local and hbhy rollout commands without changing tmux topology.
- [ ] **Step 2: Deploy the committed branch to the Telegram test runtime** and verify formatted reply, buttons, local image, local document, long-output file, and interrupt confirmation.
- [ ] **Step 3: Deploy to the hbhy Feishu runtime** and verify Card JSON 2.0 header/summary/body/footer, all native buttons, local image/document upload, Claude final content, and legacy fallback flag.
- [ ] **Step 4: Capture service logs and tmux targets** proving both channels remain tmux-backed and no uploadable absolute paths were emitted.
- [ ] **Step 5: Re-run `.venv/bin/pytest -q` and `.venv/bin/ruff check tmuxbot tests`** after any deployment correction.
- [ ] **Step 6: Record rollout evidence in the repository and commit** with `git commit -m "docs: record rich message rollout verification"`.
