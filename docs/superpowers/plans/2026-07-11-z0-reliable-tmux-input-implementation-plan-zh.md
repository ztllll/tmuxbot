# Z0 tmux 输入可靠性修复实施计划

> **Agent 执行要求：** 必须使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans`，严格执行 RED → GREEN → review。

**目标：** 修复 bracketed paste 后立即发送 Enter 导致飞书/Telegram 多行附件提示停留在 Claude/Codex 输入框的问题。

**架构：** 保留现有 per-target 锁、paste 前 busy 等待和 foreground 校验；在公共 `TmuxRuntime` 的 paste 与 Enter 之间增加可注入、可测试的 post-paste settle delay。通道 handler 不增加补偿 Enter。

**技术栈：** Python 3.10+、asyncio、tmux、pytest。

## 全局约束

- Telegram、飞书、Claude、Codex 和普通文本共用同一提交语义。
- 默认 delay 为 `0.5` 秒。
- delay 位于输入锁内，下一条消息必须等待上一条完成 Enter。
- `with_enter=False` 不 delay、不 Enter。
- 不删除 paste 前 busy 等待和前台进程校验。
- 不额外操作 Escape、Ctrl-C 或重复 Enter。

---

### Task 1：恢复 post-paste settle 语义

**Files:**
- Modify: `tmuxbot/runtime/tmux_runtime.py`
- Modify: `tmuxbot/tmux.py`
- Test: `tests/test_tmux_runtime.py`

**Interfaces:**
- Consumes: `TmuxRuntime.send_text(target, text, with_enter, expected_commands)`。
- Produces: `post_paste_delay: float = 0.5` 构造参数；顺序 `paste → sleep(delay) → Enter`。

- [ ] **Step 1：写失败测试**

扩展 fake sleep，使操作日志包含 delay：

```python
def runtime_for(fake: FakeTmux, *, post_paste_delay: float = 0.5) -> TmuxRuntime:
    async def record_sleep(delay: float) -> None:
        fake.operations.append(f"sleep:{delay}")
        await asyncio.sleep(0)

    return TmuxRuntime(
        capture_func=fake.capture,
        pane_command_func=fake.pane_command,
        paste_func=fake.paste,
        send_key_func=fake.send_key,
        busy_detector=lambda pane: pane == "busy",
        sleep_func=record_sleep,
        poll_interval=0.01,
        wait_timeout=1.0,
        post_paste_delay=post_paste_delay,
    )
```

增加：

```python
def test_paste_settles_before_enter():
    fake = FakeTmux()

    asyncio.run(runtime_for(fake).send_text("pane", "line one\nline two"))

    assert fake.operations == [
        "inspect",
        "paste:line one\nline two",
        "sleep:0.5",
        "key:Enter",
    ]


def test_without_enter_skips_settle_delay_and_key():
    fake = FakeTmux()

    asyncio.run(runtime_for(fake).send_text("pane", "draft", with_enter=False))

    assert fake.operations == ["inspect", "paste:draft"]
```

更新 busy 和并发测试期望，每条提交都必须包含 post-paste sleep。

- [ ] **Step 2：运行 RED**

Run: `uv run pytest tests/test_tmux_runtime.py -v`

Expected: FAIL，构造参数不存在或操作顺序缺少 `sleep:0.5`。

- [ ] **Step 3：最小实现**

`TmuxRuntime.__init__()` 增加：

```python
post_paste_delay: float = 0.5,
```

保存并验证非负：

```python
if post_paste_delay < 0:
    raise ValueError("post_paste_delay must be non-negative")
self.post_paste_delay = post_paste_delay
```

`send_text()` 中：

```python
await self._paste(target, text)
if with_enter:
    if self.post_paste_delay:
        await self._sleep(self.post_paste_delay)
    self._send_key(target, "Enter")
```

`tmuxbot/tmux.py` 定义：

```python
POST_PASTE_DELAY = 0.5
```

并在 `_RUNTIME` 构造时显式传入。

- [ ] **Step 4：运行 GREEN**

Run: `uv run pytest tests/test_tmux_runtime.py -v`

Expected: 全部通过。

- [ ] **Step 5：提交**

```bash
git add tmuxbot/runtime/tmux_runtime.py tmuxbot/tmux.py tests/test_tmux_runtime.py
git commit -m "fix(runtime): wait before submitting pasted input"
```

### Task 2：覆盖 Claude/Codex 多行附件 prompt

**Files:**
- Test: `tests/test_tmux_runtime.py`
- Test: `tests/test_attachments.py`

**Interfaces:**
- Consumes: `attachment_prompt()` 与 `TmuxRuntime.send_text()`。
- Produces: Claude `@path` 与 Codex `view_image` 多行 prompt 均经过一次 settle 和一次 Enter 的回归证据。

- [ ] **Step 1：增加参数化测试**

```python
@pytest.mark.parametrize("backend_name", ["claude_code", "codex"])
def test_multiline_attachment_prompt_is_submitted_after_settle(tmp_path, backend_name):
    image = tmp_path / "input.png"
    image.write_bytes(b"png")
    prompt = attachment_prompt("检查图片", [image], backend_name=backend_name)
    fake = FakeTmux()

    asyncio.run(runtime_for(fake).send_text("pane", prompt))

    assert "\n" in prompt
    assert fake.operations[-2:] == ["sleep:0.5", "key:Enter"]
    assert fake.pasted == [prompt]
```

- [ ] **Step 2：运行测试**

Run: `uv run pytest tests/test_tmux_runtime.py tests/test_attachments.py -v`

Expected: 全部通过。

- [ ] **Step 3：运行安全回归**

Run: `uv run pytest tests/test_tmux_runtime.py tests/test_feishu_replies.py tests/test_telegram_replies.py tests/e2e/test_tmux_provider_channel_matrix.py -v`

Expected: 全部通过。

- [ ] **Step 4：全量验证并提交**

Run: `make check`

Expected: compile、pytest、ruff 全部通过。

```bash
git add tests/test_tmux_runtime.py tests/test_attachments.py
git commit -m "test(runtime): cover multiline attachment submission"
git push origin productization-prep
```
