import logging

from tmuxbot.core.event_reducer import ReducedEvent
from tmuxbot.core.events import ProviderEvent, ProviderEventKind
from tmuxbot.core.runtime_v2 import RuntimeMode, RuntimeV2Router


def _event(text: str = "secret user content") -> ProviderEvent:
    return ProviderEvent(
        event_id="codex:s1:1",
        kind=ProviderEventKind.FINAL_TEXT,
        text=text,
        provider_session_id="s1",
    )


def test_off_uses_only_legacy_delivery():
    calls = []

    def legacy(event):
        calls.append("legacy")
        return [ReducedEvent("assistant_text", event.text)]

    def v2(event):
        calls.append("v2")
        return [ReducedEvent("assistant_text", event.text)]

    decision = RuntimeV2Router(RuntimeMode.OFF, legacy_reducer=legacy, v2_reducer=v2).route(
        _event()
    )

    assert calls == ["legacy"]
    assert decision.deliveries[0].body == "secret user content"
    assert decision.shadow == ()


def test_shadow_delivers_legacy_and_computes_v2():
    calls = []

    def legacy(event):
        calls.append("legacy")
        return [ReducedEvent("assistant_text", event.text)]

    def v2(event):
        calls.append("v2")
        return [ReducedEvent("assistant_text", event.text)]

    decision = RuntimeV2Router(
        RuntimeMode.SHADOW, legacy_reducer=legacy, v2_reducer=v2
    ).route(_event())

    assert calls == ["legacy", "v2"]
    assert decision.deliveries == decision.shadow
    assert decision.parity


def test_on_uses_only_v2_delivery():
    calls = []

    def legacy(event):
        calls.append("legacy")
        return [ReducedEvent("assistant_text", event.text)]

    def v2(event):
        calls.append("v2")
        return [ReducedEvent("assistant_text", event.text)]

    decision = RuntimeV2Router(RuntimeMode.ON, legacy_reducer=legacy, v2_reducer=v2).route(
        _event()
    )

    assert calls == ["v2"]
    assert decision.deliveries[0].body == "secret user content"


def test_shadow_mismatch_logs_only_redacted_structure(caplog):
    secret = "do not leak this body"
    router = RuntimeV2Router(
        RuntimeMode.SHADOW,
        legacy_reducer=lambda event: [ReducedEvent("assistant_text", event.text)],
        v2_reducer=lambda event: [ReducedEvent("assistant_tools", event.text + " extra")],
    )

    with caplog.at_level(logging.WARNING):
        decision = router.route(_event(secret))

    assert not decision.parity
    assert "runtime v2 shadow mismatch" in caplog.text
    assert secret not in caplog.text
    assert "do not leak" not in caplog.text


def test_runtime_mode_from_environment(monkeypatch):
    monkeypatch.setenv("TMUXBOT_RUNTIME_V2", "on")
    assert RuntimeV2Router.from_environment().mode == RuntimeMode.ON

    monkeypatch.setenv("TMUXBOT_RUNTIME_V2", "invalid")
    assert RuntimeV2Router.from_environment().mode == RuntimeMode.OFF
