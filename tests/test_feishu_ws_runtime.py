import asyncio
import sys
import threading
from types import SimpleNamespace

from tmuxbot.frontends.feishu import (
    _isolated_lark_ws_client_module,
    _run_isolated_lark_ws_client,
)


def test_each_feishu_frontend_gets_an_isolated_sdk_loop():
    first = _isolated_lark_ws_client_module("FEISHU")
    second = _isolated_lark_ws_client_module("FEISHU_CODEX")
    try:
        assert first is not second
        assert first.loop is not second.loop
        assert first.Client.__module__ != second.Client.__module__
    finally:
        first.loop.close()
        second.loop.close()


def test_isolated_ws_runner_disconnects_and_closes_its_loop():
    loop = asyncio.new_event_loop()
    module_name = "tmuxbot_test_isolated_lark"
    connected = threading.Event()
    receive_cancelled = threading.Event()
    disconnected = threading.Event()

    class Client:
        _auto_reconnect = True

        async def _connect(self):
            async def receive_loop():
                try:
                    while True:
                        await asyncio.sleep(3600)
                finally:
                    receive_cancelled.set()

            loop.create_task(receive_loop())
            connected.set()

        async def _disconnect(self):
            assert receive_cancelled.is_set()
            disconnected.set()

        async def _reconnect(self):
            raise AssertionError("reconnect should not run during a clean stop")

        async def _ping_loop(self):
            while True:
                await asyncio.sleep(3600)

    module = SimpleNamespace(
        __name__=module_name,
        loop=loop,
        _tmuxbot_shutdown_event=asyncio.Event(),
    )
    sys.modules[module_name] = module
    client = Client()
    worker = threading.Thread(
        target=_run_isolated_lark_ws_client,
        args=(module, client),
    )

    worker.start()
    assert connected.wait(timeout=2)
    loop.call_soon_threadsafe(module._tmuxbot_shutdown_event.set)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert receive_cancelled.is_set()
    assert disconnected.is_set()
    assert client._auto_reconnect is False
    assert loop.is_closed()
    assert module_name not in sys.modules
