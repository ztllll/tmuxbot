import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from tmuxbot import jsonl

from tmuxbot.backends.omp import OmpBackend
from tmuxbot.command_adapter import handle_semantic_action, handle_tui_action
from tmuxbot.core.events import ProviderRuntimeMetadata, TerminalState
from tmuxbot.jsonl import _capture_terminal_status
from tmuxbot.picker import detect_omp_interaction
from tmuxbot.state import Binding
from tmuxbot.tmux import _active_input_text, _is_tui_busy


FIXTURES = Path(__file__).parent / "fixtures" / "omp"


def capture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def binding(tmp_path: Path) -> Binding:
    return Binding(
        name="omp-route",
        chat_id=1,
        thread_id=None,
        tmux_session="omp-route",
        tmux_window=0,
        tmux_pane=0,
        cwd=tmp_path,
        backend="omp",
    )


def test_idle_footer_parses_only_native_omp_17_3_2_fields():
    status = OmpBackend().parse_terminal_status(capture("idle.txt"))

    assert status is not None
    assert status.state == TerminalState.IDLE
    assert status.provider == "AISuperToken"
    assert status.model == "GPT-5.6 Sol"
    assert status.effort == "high"
    assert status.cwd == "~/claude-project/tmuxbot"
    assert status.git_branch == "main"
    assert status.session_name is None
    assert status.context_percent == 42.0
    assert status.context_limit == 360_000
    assert status.context_used == 151_200
    assert status.cost_usd == pytest.approx(0.125)


def test_current_omp_status_bar_keeps_native_labels_order_and_fields(tmp_path, monkeypatch):
    class MetadataBackend(OmpBackend):
        def current_runtime_metadata(self, _binding):
            return ProviderRuntimeMetadata(
                provider="aisupertoken",
                model="gpt-5.6-sol",
                effort="xhigh",
                session_name="stale transcript title",
                input_tokens=826_804,
                output_tokens=96_813,
                cache_read_tokens=59_213_696,
                cache_hit_rate=0.9847,
                cost_usd=57.16,
            )

    pane = capture("current_status_bar.txt")
    monkeypatch.setattr(jsonl, "tmux_capture", lambda *_args: pane)
    backend = MetadataBackend()
    status = asyncio.run(_capture_terminal_status(binding(tmp_path), backend))

    assert status is not None
    assert status.provider == "AISuperToken"
    assert status.model == "GPT-5.6 Sol"
    assert status.session_name == "将 todo 也改为中文"
    assert status.auto_compact is True
    status = replace(status, session_file_size_bytes=13_000_000)
    assert backend.format_status_footer(status) == (
        "模型 GPT-5.6 Sol (AISuperToken) · 上下文 48.8%/360K · 思考 xhigh · JSON 13.0MB"
    )


def test_compact_omp_status_bar_uses_the_same_semantic_contract():
    pane = capture("status_bar_v17_3_3.txt")
    backend = OmpBackend()
    status = backend.parse_terminal_status(pane)

    assert status is not None
    assert status.state == TerminalState.WORKING
    assert status.label == "- 正在分析新版状态栏 [esc]"
    assert status.provider == "AISuperToken"
    assert status.model == "GPT-5.6 Sol"
    assert status.effort == "xhigh"
    assert status.context_percent == 66.8
    assert status.context_limit == 360_000
    assert status.auto_compact is True
    assert _is_tui_busy(pane)

    status = replace(status, session_file_size_bytes=13_000_000)
    assert backend.format_status_footer(status) == (
        "模型 GPT-5.6 Sol (AISuperToken) · 上下文 66.8%/360K · 思考 xhigh · JSON 13.0MB"
    )


def test_compact_footer_uses_runtime_context_when_width_hides_ctx(tmp_path, monkeypatch):
    class MetadataBackend(OmpBackend):
        def current_runtime_metadata(self, _binding):
            return ProviderRuntimeMetadata(
                provider="aisupertoken",
                model="gpt-5.6-sol",
                effort="xhigh",
                context_used=274_520,
                context_limit=360_000,
                context_percent=274_520 / 360_000 * 100,
            )

    pane = capture("status_bar_v17_3_3_narrow.txt")
    monkeypatch.setattr(jsonl, "tmux_capture", lambda *_args: pane)
    backend = MetadataBackend()
    status = asyncio.run(_capture_terminal_status(binding(tmp_path), backend))

    assert status is not None
    assert status.label == "\\ 正在处理窄状态栏 [esc]"
    assert status.state == TerminalState.WORKING
    assert _is_tui_busy(pane)
    assert status.provider == "AISuperToken"
    assert status.model == "GPT-5.6 Sol"
    assert status.context_used == 274_520
    assert status.context_limit == 360_000
    assert status.context_percent == pytest.approx(76.2555, rel=1e-4)
    assert backend.format_status_footer(status) == (
        "模型 GPT-5.6 Sol (AISuperToken) · 上下文 76.3%/360K · 思考 xhigh"
    )


def test_active_loader_is_current_and_historical_loader_text_is_not():
    active = capture("active_loader.txt")
    status = OmpBackend().parse_terminal_status(active)

    assert status is not None
    assert status.state == TerminalState.WORKING
    assert status.label.endswith("⟦esc⟧")
    assert _is_tui_busy(active)
    assert not _is_tui_busy(
        "⠼ Working… old history ⟦esc⟧\n"
        "╭── π OMP 17.3.2 ╮\n"
        "unrelated live line\n"
        "╰─ 10%/128k • high ─╯\n"
    )
    assert (
        OmpBackend().parse_terminal_status("░▒▓ 🤖 old-model 🪟 ctx 10%/128k\n📄 JSONL 1 MB\n")
        is None
    )


def test_narrow_footer_and_composer_keep_only_provable_fields():
    pane = capture("narrow_pane.txt")
    status = OmpBackend().parse_terminal_status(pane)

    assert status is not None
    assert status.context_percent == 71.0
    assert status.context_limit == 128_000
    assert status.effort == "high"
    assert status.cwd is None
    assert status.git_branch is None
    assert _active_input_text(pane) == "short draft"


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("ask_approval.txt", "confirmation"),
        ("resume_picker.txt", "selection"),
        ("model_picker.txt", "selection"),
        ("plan_review.txt", "confirmation"),
    ],
)
def test_live_interactions_require_current_footer_or_bottom_modal(name, kind):
    interaction = detect_omp_interaction(capture(name))

    assert interaction is not None
    assert interaction.kind == kind


def test_distant_or_stale_interaction_controls_fail_closed():
    assert (
        detect_omp_interaction(
            "→ old choice\n↑/↓ navigate • enter select • esc close\n"
            "assistant prose\nmore prose\nlatest prose\n"
            "╭── π OMP 17.3.2 ╮\n╰─ 10%/128k • high ─╯\n"
        )
        is None
    )
    assert (
        detect_omp_interaction(capture("resume_picker.txt") + "new output after stale footer\n")
        is None
    )
    assert (
        detect_omp_interaction(capture("model_picker.txt") + "new output after stale modal\n")
        is None
    )


def test_omp_backend_exposes_exact_remote_interaction_policy(tmp_path):
    backend = OmpBackend()

    assert set(backend.interactive_commands()) == {
        "/login",
        "/model",
        "/scoped-models",
        "/settings",
        "/statusline",
        "/resume",
        "/tree",
        "/trust",
        "/fork",
        "/import",
    }
    assert "/plan" not in backend.interactive_commands()
    assert backend.remote_tui_actions_allowed is False
    assert backend.requires_idle_for_control_commands is True
    assert binding(tmp_path).tmux_target in backend.format_remote_interaction_notice(
        binding(tmp_path), "选择菜单"
    )


def test_omp_remote_tui_actions_send_zero_keys(tmp_path, monkeypatch):
    item = binding(tmp_path)
    backend = OmpBackend()
    keys = []
    notices = []

    class Frontend:
        async def send_html(self, _chat_id, _thread_id, text):
            notices.append(text)

        async def send_interaction_card(self, *_args):
            raise AssertionError("OMP must not render remote TUI controls")

    monkeypatch.setattr(
        "tmuxbot.command_adapter.tmux_send_key",
        lambda target, key: keys.append((target, key)),
    )

    import asyncio

    asyncio.run(handle_tui_action(Frontend(), item, 1, None, "enter", backend=backend, state=None))
    asyncio.run(handle_semantic_action(Frontend(), backend, item, 1, None, "approve-plan"))

    assert keys == []
    assert len(notices) == 2
    assert all("SSH" in notice for notice in notices)


def test_local_plan_access_notice_does_not_claim_a_detected_interaction(tmp_path):
    backend = OmpBackend()
    notice = backend.format_remote_access_notice(binding(tmp_path), "切换 Plan 模式")

    assert "已确认" not in notice
    assert "如需切换 Plan 模式" in notice
    assert "SSH" in notice
