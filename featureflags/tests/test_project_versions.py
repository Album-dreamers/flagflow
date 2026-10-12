"""
Version changes reach SDK clients through LISTEN/NOTIFY and SSE, and are
not lost while the listener is reconnecting.
"""

import asyncio
import json
import socket
from contextlib import AsyncExitStack, asynccontextmanager, suppress

import aiopg
import httpx
import pytest
import uvicorn
from sqlalchemy import select, text, update

from featureflags.config import config
from featureflags.graph import actions
from featureflags.models import Project
from featureflags.services.project_versions import (
    CHANNEL,
    ProjectVersionHub,
)
from featureflags.tests.state import mk_auth_user, mk_flag, mk_project, mk_value
from featureflags.tests.test_save_flag_consistency import (
    SAVE_FLAG,
    enable,
    failing_step,
    scalar,
)

DELETE_FLAG = """
    mutation DeleteFlag($id: String!) { deleteFlag(id: $id) { error } }
"""
DELETE_VALUE = """
    mutation DeleteValue($id: String!) { deleteValue(id: $id) { error } }
"""

TIMEOUT = 5


@asynccontextmanager
async def listener():
    async with aiopg.connect(config.postgres.dsn) as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(f"LISTEN {CHANNEL}")
        yield conn.notifies


async def next_notification(notifies):
    notify = await asyncio.wait_for(notifies.get(), TIMEOUT)
    return json.loads(notify.payload)


async def no_notification(notifies):
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(notifies.get(), 0.3)


async def set_version(db_engine, project, version):
    # what a save committed by another instance looks like to this one
    async with db_engine.acquire() as conn:
        await conn.execute(
            update(Project.__table__)
            .where(Project.id == project.id)
            .values({Project.version: version})
        )


async def wait_version(subscription, within=TIMEOUT):
    return await asyncio.wait_for(subscription.wait(), within)


@asynccontextmanager
async def running(hub):
    task = asyncio.create_task(hub.run())
    try:
        await asyncio.wait_for(hub.listening.wait(), TIMEOUT)
        yield hub
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def make_hub(db_engine, **kwargs):
    kwargs.setdefault("reconnect_delay", 0.05)
    return ProjectVersionHub(db_engine, config.postgres.dsn, **kwargs)


# Writer side


async def test_save_notifies_new_version_on_commit(db_engine, graphql):
    user = await mk_auth_user(db_engine)
    project = await mk_project(db_engine, version=3)
    flag = await mk_flag(db_engine, enabled=False, project=project)

    async with listener() as notifies:
        await graphql(user, SAVE_FLAG, operations=enable(flag))

        assert await next_notification(notifies) == {
            "project": project.name,
            "version": 4,
        }


async def test_failed_save_sends_no_notification(
    monkeypatch, db_engine, graphql
):
    # the version bump ran, but the transaction was rolled back afterwards
    user = await mk_auth_user(db_engine)
    project = await mk_project(db_engine, version=0)
    flag = await mk_flag(db_engine, enabled=False, project=project)
    monkeypatch.setattr(actions, "update_changelog", failing_step)

    async with listener() as notifies:
        with pytest.raises(RuntimeError):
            await graphql(user, SAVE_FLAG, operations=enable(flag))

        await no_notification(notifies)


@pytest.mark.parametrize(
    "mutation, make",
    [(DELETE_FLAG, mk_flag), (DELETE_VALUE, mk_value)],
    ids=["flag", "value"],
)
async def test_delete_bumps_project_version(
    mutation, make, db_engine, conn, graphql
):
    # otherwise clients keep evaluating the deleted flag until the project
    # changes for some other reason
    user = await mk_auth_user(db_engine)
    project = await mk_project(db_engine, version=3)
    item = await make(db_engine, project=project)

    async with listener() as notifies:
        result = await graphql(user, mutation, id=str(item.id))
        assert "errors" not in result

        assert await next_notification(notifies) == {
            "project": project.name,
            "version": 4,
        }
    assert (
        await scalar(
            conn, select([Project.version]).where(Project.id == project.id)
        )
        == 4
    )


# Hub


async def test_subscribe_sends_current_version_if_client_is_behind(
    db_engine,
):
    project = await mk_project(db_engine, version=5)
    hub = make_hub(db_engine)

    async with hub.subscribe(project.name, 3) as subscription:
        assert await wait_version(subscription) == 5


async def test_subscribe_waits_if_client_is_up_to_date(db_engine):
    project = await mk_project(db_engine, version=5)
    hub = make_hub(db_engine)

    async with hub.subscribe(project.name, 5) as subscription:
        with pytest.raises(asyncio.TimeoutError):
            await wait_version(subscription, within=0.3)


async def test_listener_wakes_subscribers_of_changed_project(
    db_engine, graphql
):
    user = await mk_auth_user(db_engine)
    project = await mk_project(db_engine, version=0)
    other = await mk_project(db_engine, version=0)
    flag = await mk_flag(db_engine, enabled=False, project=project)

    async with (
        running(make_hub(db_engine)) as hub,
        hub.subscribe(project.name, 0) as subscription,
        hub.subscribe(other.name, 0) as other_subscription,
    ):
        await graphql(user, SAVE_FLAG, operations=enable(flag))

        assert await wait_version(subscription) == 1
        with pytest.raises(asyncio.TimeoutError):
            await wait_version(other_subscription, within=0.3)


async def test_slow_subscriber_gets_only_latest_version(db_engine):
    project = await mk_project(db_engine, version=0)
    hub = make_hub(db_engine)

    async with hub.subscribe(project.name, 0) as subscription:
        for version in (1, 2, 3):
            hub.publish(project.name, version)
        hub.publish(project.name, 2)  # out of order, must not go back

        assert await wait_version(subscription) == 3
        with pytest.raises(asyncio.TimeoutError):
            await wait_version(subscription, within=0.1)


async def test_change_during_listener_outage_is_recovered(db_engine):
    project = await mk_project(db_engine, version=0)
    hub = make_hub(db_engine, reconnect_delay=0.5)

    async with (
        running(hub),
        hub.subscribe(project.name, 0) as subscription,
    ):
        async with db_engine.acquire() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE query = :listen"
                ),
                listen=f"LISTEN {CHANNEL}",
            )
        # committed while the hub waits to reconnect: the NOTIFY is lost
        await asyncio.sleep(0.1)
        assert not hub.listening.is_set()
        await set_version(db_engine, project, 7)

        assert await wait_version(subscription) == 7
        assert hub.listening.is_set()


def count_queries(hub, monkeypatch):
    queries = []
    fetch = hub._fetch_versions

    async def counting(projects):
        if projects:
            queries.append(projects)
        return await fetch(projects)

    monkeypatch.setattr(hub, "_fetch_versions", counting)
    return queries


async def bump(db_engine, project):
    async with db_engine.acquire() as conn, conn.begin():
        await actions.bump_project_versions(Project.id == project.id, conn=conn)


async def test_new_streams_use_the_known_version(db_engine, monkeypatch):
    # thousands of clients connecting must not each query the database
    project = await mk_project(db_engine, version=3)
    hub = make_hub(db_engine)
    queries = count_queries(hub, monkeypatch)

    async with running(hub):
        async with AsyncExitStack() as stack:
            subscriptions = await asyncio.gather(
                *(
                    stack.enter_async_context(hub.subscribe(project.name, 0))
                    for _ in range(50)
                )
            )
            for subscription in subscriptions:
                assert await wait_version(subscription) == 3
        assert len(queries) == 1

        await bump(db_engine, project)
        async with hub.subscribe(project.name, 0) as subscription:
            assert await wait_version(subscription) == 4
        assert len(queries) == 1


async def test_known_versions_are_dropped_on_reconnect(db_engine):
    project = await mk_project(db_engine, version=3)
    hub = make_hub(db_engine, reconnect_delay=0.5)

    async with running(hub):
        async with hub.subscribe(project.name, 0) as subscription:
            assert await wait_version(subscription) == 3

        async with db_engine.acquire() as conn:
            await conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE query = :listen"
                ),
                listen=f"LISTEN {CHANNEL}",
            )
        await asyncio.sleep(0.1)
        assert not hub.listening.is_set()
        # no subscriber, so nothing reconciles this project on reconnect
        await set_version(db_engine, project, 7)
        await asyncio.wait_for(hub.listening.wait(), TIMEOUT)

        async with hub.subscribe(project.name, 0) as subscription:
            assert await wait_version(subscription) == 7


async def test_readonly_hub_polls(db_engine):
    project = await mk_project(db_engine, version=0)
    hub = make_hub(db_engine, listen=False, poll_interval=0.05)

    async with (
        running(hub),
        hub.subscribe(project.name, 0) as subscription,
    ):
        await set_version(db_engine, project, 2)

        assert await wait_version(subscription) == 2


# SSE endpoint, over a real server so the response is actually streamed


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def http_server():
    from featureflags.http.app import create_app

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            # long enough that a stream closed in time was closed by us
            timeout_graceful_shutdown=30,
        )
    )
    server.url = f"http://127.0.0.1:{port}"
    server.task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 (uvicorn has no event for it)
        await asyncio.sleep(0.01)
    try:
        yield server
    finally:
        server.should_exit = True
        await server.task


async def sse_events(response):
    event = {}
    async for line in response.aiter_lines():
        if line.startswith(("event: ", "id: ")):
            key, _, value = line.partition(": ")
            event[key] = value
        elif line.startswith("data: "):
            event["data"] = json.loads(line.removeprefix("data: "))
        elif not line and event:
            yield event
            event = {}


def version_event(version):
    return {
        "event": "version",
        "id": str(version),
        "data": {"version": version},
    }


@asynccontextmanager
async def stream(server, project, **kwargs):
    async with (
        httpx.AsyncClient(base_url=server.url, timeout=TIMEOUT) as client,
        client.stream(
            "GET",
            "/flags/stream",
            params={"project": project.name, **kwargs.pop("params", {})},
            **kwargs,
        ) as response,
    ):
        assert response.headers["content-type"].startswith("text/event-stream")
        yield sse_events(response)


async def next_event(events, within=TIMEOUT):
    return await asyncio.wait_for(anext(events), within)


async def test_stream_endpoint(db_engine, graphql, http_server):
    user = await mk_auth_user(db_engine)
    project = await mk_project(db_engine, version=2)
    flag = await mk_flag(db_engine, enabled=False, project=project)

    async with stream(http_server, project, params={"version": 1}) as events:
        # client was behind when it connected
        assert await next_event(events) == version_event(2)

        await graphql(user, SAVE_FLAG, operations=enable(flag))

        assert await next_event(events) == version_event(3)


async def test_stream_resumes_from_last_event_id(db_engine, http_server):
    project = await mk_project(db_engine, version=2)

    # without the header the client would count as behind (version 0)
    async with stream(
        http_server, project, headers={"Last-Event-ID": "2"}
    ) as events:
        with pytest.raises(asyncio.TimeoutError):
            await next_event(events, within=0.3)


async def test_shutdown_closes_open_streams(db_engine, http_server):
    project = await mk_project(db_engine, version=1)

    async with stream(http_server, project, params={"version": 1}) as events:
        http_server.should_exit = True

        # the stream ends instead of holding shutdown until the timeout
        with pytest.raises(StopAsyncIteration):
            await next_event(events)
        await asyncio.wait_for(http_server.task, TIMEOUT)
