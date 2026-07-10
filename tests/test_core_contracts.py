from tmuxbot.core.capabilities import ChannelCapabilities, ProviderCapabilities
from tmuxbot.core.events import (
    ProviderEvent,
    ProviderEventKind,
    TerminalState,
    TerminalStatus,
)
from tmuxbot.core.messages import IncomingMessage
from tmuxbot.core.replies import ReplyEnvelope
from tmuxbot.core.sessions import SessionIdentity


def test_core_contracts_are_provider_and_channel_neutral():
    status = TerminalStatus(
        state=TerminalState.IDLE,
        label="ready",
        model="gpt-5",
        permission_mode="yolo",
        cwd="/repo",
    )
    event = ProviderEvent(
        event_id="session:1",
        kind=ProviderEventKind.FINAL_TEXT,
        text="done",
        status=status,
    )
    reply = ReplyEnvelope(title="Reply", body=event.text, footer=status)
    incoming = IncomingMessage(
        source_id="chat",
        thread_id="topic",
        sender_id="boss",
        text="go",
    )
    session = SessionIdentity(
        provider="codex",
        session_id="abc",
        transcript_path="/tmp/a.jsonl",
    )

    assert reply.footer is status
    assert incoming.text == "go"
    assert session.provider == "codex"
    assert ProviderCapabilities(name="codex").name == "codex"
    assert ChannelCapabilities(name="telegram", supports_edit=True).supports_edit


def test_provider_event_and_reply_metadata_are_immutable():
    event = ProviderEvent(
        event_id="session:2",
        kind=ProviderEventKind.TOOL_PROGRESS,
        text="reading",
        metadata={"tool": "Read"},
    )
    reply = ReplyEnvelope(
        title="Reply",
        body="reading",
        actions=("cancel",),
        attachments=("/tmp/result.txt",),
    )

    assert event.metadata == {"tool": "Read"}
    assert reply.actions == ("cancel",)
    assert reply.attachments == ("/tmp/result.txt",)
