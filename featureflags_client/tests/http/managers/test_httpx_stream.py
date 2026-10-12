import asyncio
import json
from functools import partial

import httpx
import pytest

from featureflags_client.http.managers import httpx as httpx_manager
from featureflags_client.http.managers.httpx import HttpxManager, jittered
from featureflags_client.http.utils import intervals_gen

TIMEOUT = 2


class Defaults:
    TEST = False


class FakeServer:
    """
    /flags/sync answers with `version`; /flags/stream relays what the test
    puts into the queue of the current connection (None closes it).
    """

    def __init__(self, version=1, stream_failures=0):
        self.version = version
        self.stream_failures = stream_failures
        self.sync_versions = []
        self.stream_params = []
        self.events = None
        self.connected = asyncio.Event()

    async def push(self, version):
        await self.events.put(version)

    async def _event_stream(self, events):
        while (version := await events.get()) is not None:
            data = json.dumps({"version": version})
            yield f"event: version\ndata: {data}\n\n".encode()

    async def handle(self, request):
        if request.url.path == "/flags/sync":
            self.sync_versions.append(json.loads(request.content)["version"])
            return httpx.Response(
                200, json={"version": self.version, "flags": [], "values": []}
            )

        assert request.url.path == "/flags/stream"
        self.stream_params.append(dict(request.url.params))
        if self.stream_failures:
            self.stream_failures -= 1
            return httpx.Response(503)
        self.events = asyncio.Queue()
        self.connected.set()
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=self._event_stream(self.events),
        )


def make_manager(server, **kwargs):
    manager = HttpxManager(
        url="http://flags.server.example",
        project="test",
        variables=[],
        defaults=Defaults,
        **kwargs,
    )
    manager._session = httpx.AsyncClient(
        base_url="http://flags.server.example",
        transport=httpx.MockTransport(server.handle),
    )
    return manager


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(
        httpx_manager,
        "intervals_gen",
        partial(intervals_gen, retry_interval_min=0.01),
    )


async def wait_for(condition):
    async def poll():
        while not condition():  # noqa: ASYNC110 (state has no event)
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), TIMEOUT)


@pytest.mark.asyncio
async def test_new_version_event_triggers_sync():
    server = FakeServer(version=1)
    manager = make_manager(server, refresh_interval=3600, stream=True)
    manager.start()
    await wait_for(lambda: manager._state.version == 1)
    await asyncio.wait_for(server.connected.wait(), TIMEOUT)

    server.version = 2
    await server.push(2)

    await wait_for(lambda: manager._state.version == 2)
    await manager.wait_closed()


@pytest.mark.asyncio
async def test_outdated_version_event_is_ignored():
    server = FakeServer(version=3)
    manager = make_manager(server, stream=True)
    manager._state.version = 3
    task = asyncio.create_task(manager._stream_loop())
    await asyncio.wait_for(server.connected.wait(), TIMEOUT)

    await server.push(2)
    await server.push(3)
    await server.push(4)

    await wait_for(lambda: server.sync_versions)
    assert server.sync_versions == [3]  # only the event for version 4

    task.cancel()
    await manager.close()


@pytest.mark.asyncio
async def test_reconnects_after_failures_with_current_version():
    server = FakeServer(version=5, stream_failures=2)
    manager = make_manager(server, stream=True)
    manager._state.version = 4
    task = asyncio.create_task(manager._stream_loop())

    await asyncio.wait_for(server.connected.wait(), TIMEOUT)
    assert len(server.stream_params) == 3
    assert server.stream_params[-1] == {"project": "test", "version": "4"}

    # server restart: stream closes, the client comes back with the
    # version it has now, so the server can tell it what it missed
    await server.push(5)
    await wait_for(lambda: manager._state.version == 5)
    server.connected.clear()
    await server.events.put(None)

    await asyncio.wait_for(server.connected.wait(), TIMEOUT)
    assert server.stream_params[-1] == {"project": "test", "version": "5"}

    task.cancel()
    await manager.close()


@pytest.mark.asyncio
async def test_stream_is_off_by_default():
    server = FakeServer()
    manager = make_manager(server, refresh_interval=3600)
    manager.start()
    await wait_for(lambda: manager._state.version == 1)

    assert manager._stream_task is None
    assert server.stream_params == []
    await manager.wait_closed()


def test_retry_intervals_back_off():
    retry = intervals_gen()
    retry.send(None)

    assert [retry.send(False) for _ in range(7)] == [1, 2, 4, 8, 16, 32, 32]
    retry.send(True)
    assert retry.send(False) == 1


def test_reconnect_delays_are_spread():
    delays = [jittered(8) for _ in range(1000)]

    assert all(4 <= delay <= 8 for delay in delays)
    assert max(delays) - min(delays) > 3
