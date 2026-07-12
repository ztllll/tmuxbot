# Provider Session Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make channel-issued `/new` and `/clear` switch a tmux binding to its new provider transcript without losing cross-binding isolation.

**Architecture:** The command layer sets a transient handoff boundary before it injects a command expected to create a new provider session. Provider transcript lookup uses that boundary only to choose a newer same-cwd transcript. The jsonl tailer persists the selected identity and clears the boundary after a successful switch.

**Tech Stack:** Python 3.14, pytest, existing Binding/backend/jsonl abstractions.

## Global Constraints

- Never choose a global newest transcript; candidates must match the binding project directory.
- Existing persisted identity remains the default when no handoff is pending.
- Do not persist a pending handoff marker to `bindings.yaml`.
- Add red/green regression coverage for Codex and Claude.

---

### Task 1: Define and verify pending handoff selection

**Files:**
- Modify: `tmuxbot/state.py`
- Modify: `tmuxbot/backends/codex.py`
- Modify: `tmuxbot/backends/claude_code.py`
- Test: `tests/test_session_identity.py`

- [ ] **Step 1: Write failing tests**

```python
def test_codex_pending_handoff_prefers_newer_same_cwd_transcript(...):
    binding.pending_session_handoff_after = 10.0
    assert CodexBackend().find_active_jsonl(binding) == newer

def test_claude_pending_handoff_prefers_newer_project_transcript(...):
    binding.pending_session_handoff_after = 10.0
    assert ClaudeCodeBackend().find_active_jsonl(binding) == newer
```

- [ ] **Step 2: Run tests to verify RED**

Run: `uv run pytest tests/test_session_identity.py -q`

- [ ] **Step 3: Implement minimal handoff selector**

Add `pending_session_handoff_after: float | None` to `Binding`. In each backend, when present, choose the newest same-project transcript whose `st_mtime` is at least the boundary and whose identity is not the current provider ID; otherwise use current pinned behavior.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `uv run pytest tests/test_session_identity.py -q`

### Task 2: Arm and complete the handoff lifecycle

**Files:**
- Modify: `tmuxbot/commands.py`
- Modify: `tmuxbot/jsonl.py`
- Test: `tests/test_commands.py` or existing command test module
- Test: `tests/test_jsonl.py`

- [ ] **Step 1: Write failing tests**

```python
async def test_new_command_arms_handoff_before_polling(...):
    await capture_and_push(..., command="/new")
    assert binding.pending_session_handoff_after is not None

async def test_jsonl_switch_clears_pending_handoff_after_identity_saved(...):
    ...
    assert binding.pending_session_handoff_after is None
```

- [ ] **Step 2: Run focused tests to verify RED**

Run: `uv run pytest tests/test_commands.py tests/test_jsonl.py -q`

- [ ] **Step 3: Implement minimal lifecycle**

Set `pending_session_handoff_after = time.time()` immediately before emitting any command whose `CmdOpts.expect_new_session` is true. Clear it in the jsonl tailer only when a distinct transcript has been adopted and identity persistence has been scheduled.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run: `uv run pytest tests/test_commands.py tests/test_jsonl.py -q`

### Task 3: Regression verification and delivery

**Files:**
- Modify: regression tests from Tasks 1-2

- [ ] **Step 1: Run project checks**

Run: `make check`
Expected: all tests and ruff pass.

- [ ] **Step 2: Run direct restart-resume regression**

Run: `uv run pytest tests/test_codex_backend.py tests/test_session_identity.py -q`
Expected: new identity is selected after `/new` and `ensure_running()` resumes that ID.

- [ ] **Step 3: Commit and deploy**

```bash
git add tmuxbot tests docs/superpowers/specs docs/superpowers/plans
git commit -m "fix: hand off bindings after new provider session"
uv build --wheel
uv tool install --force 'dist/tmuxbot-0.3.0-py3-none-any.whl[full]'
```
