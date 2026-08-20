import asyncio
from pathlib import Path
from types import SimpleNamespace

from tmuxbot.core.events import TerminalState, TerminalStatus
from tmuxbot.jsonl import on_tmux_event
from tmuxbot.state import Binding, State


class Backend:
    name = "pi"

    def read_tasks(self, _binding):
        return []

    def render_extension_footer(self, _binding):
        return ""

    def parse_terminal_status(self, _screen):
        return TerminalStatus(state=TerminalState.IDLE)

    def current_runtime_metadata(self, _binding):
        return SimpleNamespace(
            provider=None, model=None, effort=None, permission_mode=None,
            session_name=None, input_tokens=None, output_tokens=None,
            cache_read_tokens=None, cache_write_tokens=None, cache_hit_rate=None,
            cost_usd=None,
        )


class Frontend:
    async def send_assistant_reply(self, binding, _envelope):
        self.binding = binding
        return None

    async def send_html(self, *_args):
        return None


def test_topic_originated_admin_reply_uses_verified_feishu_endpoint(monkeypatch):
    binding = Binding(
        name="tmuxbot-admin", chat_id="oc_admin", thread_id=None,
        tmux_session="pi-tmuxbot-admin", tmux_window=0, tmux_pane=0,
        cwd=Path("/tmp/admin"), backend="pi", channel="feishu", admin=True,
    )
    state = State()
    state.admin_delivery_contexts[binding.name] = {
        "chat_id": "oc_project", "thread_id": "omt_topic", "thread_root_message_id": "om_root",
    }
    frontend = Frontend()
    monkeypatch.setattr("tmuxbot.jsonl.tmux_capture", lambda *_args: "")

    asyncio.run(on_tmux_event(binding, "assistant_text", "已生成项目开通计划", frontend, state, Backend()))

    assert frontend.binding.chat_id == "oc_project"
    assert frontend.binding.thread_id == "omt_topic"
    assert frontend.binding.thread_root_message_id == "om_root"
    assert binding.name not in state.admin_delivery_contexts
