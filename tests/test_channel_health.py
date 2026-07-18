from __future__ import annotations

import asyncio
import json

from tmuxbot.channel_health import ChannelHealthRegistry, channel_health_audit_loop


def test_registry_records_the_same_contract_for_every_channel(tmp_path) -> None:
    registry = ChannelHealthRegistry()
    for channel_id, channel, scope in (
        ("telegram:TG_CODEX_BOT_TOKEN", "telegram", "TG_CODEX_BOT_TOKEN"),
        ("feishu:FEISHU_CODEX", "feishu", "FEISHU_CODEX"),
    ):
        registry.register(
            channel_id, channel=channel, credential_scope=scope, binding_count=1
        )
        registry.connected(channel_id)
        registry.transport_activity(channel_id)
        registry.inbound(channel_id)

    registry.recovering("feishu:FEISHU_CODEX", "connection refresh")
    registry.write(tmp_path / "channel-health.json")

    audit = json.loads((tmp_path / "channel-health.json").read_text())
    by_id = {item["id"]: item for item in audit["channels"]}
    assert by_id["telegram:TG_CODEX_BOT_TOKEN"]["state"] == "connected"
    assert by_id["telegram:TG_CODEX_BOT_TOKEN"]["last_inbound_at"] is not None
    assert by_id["feishu:FEISHU_CODEX"]["state"] == "recovering"
    assert by_id["feishu:FEISHU_CODEX"]["recovery_count"] == 1


def test_audit_loop_persists_final_stopped_snapshot(tmp_path) -> None:
    registry = ChannelHealthRegistry()
    registry.register("telegram:TG", channel="telegram", credential_scope="TG", binding_count=1)
    registry.connected("telegram:TG")
    path = tmp_path / "channel-health.json"

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(channel_health_audit_loop(registry, path, stop, interval=60))
        for _ in range(20):
            if path.exists():
                break
            await asyncio.sleep(0.01)
        registry.stopped("telegram:TG")
        stop.set()
        await task

    asyncio.run(scenario())
    audit = json.loads(path.read_text())
    assert audit["channels"][0]["state"] == "stopped"
