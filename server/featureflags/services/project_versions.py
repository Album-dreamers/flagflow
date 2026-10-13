"""
    Push project version changes to SDK clients.

    Writers call ``notify_project_version`` inside the transaction that bumps
    ``project.version``; Postgres delivers a NOTIFY only when that transaction
    commits, so a rolled back save never reaches clients.

    Every http instance runs one ``ProjectVersionHub``: it LISTENs on the
    channel over a dedicated connection and wakes up the SSE streams of the
    affected project. Notifications are only a latency optimization, the
    database stays the source of truth: the hub re-reads the versions of
    watched projects after every (re)connect and every ``reconcile_interval``
    seconds, so a notification lost while the listener was down is recovered.
    A readonly replica can't LISTEN, there the hub only polls.
"""
import asyncio
import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress

import aiopg
import aiopg.sa
import anyio
from aiopg.sa import SAConnection
from prometheus_client import Counter, Gauge
from sqlalchemy import func, select

from featureflags.config import config
from featureflags.models import Project

log = logging.getLogger(__name__)

CHANNEL = "featureflags_project_version"

SSE_STREAMS_GAUGE = Gauge(
    "sse_streams",
    "Number of open SSE streams on this instance.",
)
LISTENER_RECONNECTS_COUNTER = Counter(
    "project_version_listener_reconnects",
    "Number of times the LISTEN connection had to be re-established.",
)


async def notify_project_version(
    conn: SAConnection, project: str, version: int
) -> None:
    payload = json.dumps({"project": project, "version": version})
    await conn.execute(select([func.pg_notify(CHANNEL, payload)]))


class Subscription:
    """
    Latest known version of one project for one stream.

    Only the latest version matters to a client, so pushes are coalesced:
    a slow stream never accumulates a backlog.
    """

    def __init__(self, version: int) -> None:
        self.version = version
        self._changed = asyncio.Event()

    def push(self, version: int) -> None:
        if version > self.version:
            self.version = version
            self._changed.set()

    async def wait(self, until: anyio.Event | None = None) -> int | None:
        """
        Next newer version, or None once `until` is set.
        """
        waiters = {asyncio.ensure_future(self._changed.wait())}
        if until is not None:
            waiters.add(asyncio.ensure_future(until.wait()))
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        if not self._changed.is_set():
            return None
        self._changed.clear()
        return self.version


class ProjectVersionHub:
    def __init__(
        self,
        db_engine: aiopg.sa.Engine,
        dsn: str,
        *,
        listen: bool = True,
        reconcile_interval: float = 30.0,
        poll_interval: float = 1.0,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
        stable_after: float = 60.0,
        timeout: float = 10.0,
    ) -> None:
        self._db_engine = db_engine
        self._dsn = dsn
        self._listen = listen
        self._reconcile_interval = reconcile_interval
        self._poll_interval = poll_interval
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._stable_after = stable_after
        self._listening_since: float | None = None
        self._timeout = timeout
        self._subscriptions: dict[str, set[Subscription]] = defaultdict(set)
        self.listening = asyncio.Event()
        # Latest version per project, kept only while LISTEN is up (every
        # change then reaches us), so new streams don't each query the
        # database. Cleared and bumped to a new generation on reconnect.
        self._versions: dict[str, int] = {}
        self._generation = 0
        self._fetches: dict[str, asyncio.Future] = {}

    @asynccontextmanager
    async def subscribe(
        self, project: str, version: int
    ) -> AsyncIterator[Subscription]:
        """
        Watch a project, starting from the version the client already has.

        The current version is read after registering, so an update committed
        in between is seen either by this read or by the listener.
        """
        subscription = Subscription(version)
        self._subscriptions[project].add(subscription)
        SSE_STREAMS_GAUGE.inc()
        try:
            current = await self._current_version(project)
            if current is not None:
                subscription.push(current)
            yield subscription
        finally:
            subscriptions = self._subscriptions[project]
            subscriptions.discard(subscription)
            if not subscriptions:
                del self._subscriptions[project]
            SSE_STREAMS_GAUGE.dec()

    def publish(self, project: str, version: int) -> None:
        if self._listen:
            self._remember(project, version)
        for subscription in self._subscriptions.get(project, ()):
            subscription.push(version)

    def _remember(self, project: str, version: int) -> None:
        # max: a slow database read must not undo a newer notification
        if version > self._versions.get(project, -1):
            self._versions[project] = version

    async def _current_version(self, project: str) -> int | None:
        if self.listening.is_set() and project in self._versions:
            return self._versions[project]
        # streams connecting at once to the same project share one query
        fetch = self._fetches.get(project)
        if fetch is None:
            fetch = asyncio.ensure_future(self._fetch_version(project))
            self._fetches[project] = fetch
            fetch.add_done_callback(lambda _: self._fetches.pop(project, None))
        return await asyncio.shield(fetch)

    async def _fetch_version(self, project: str) -> int | None:
        generation, listening = self._generation, self.listening.is_set()
        rows = await self._fetch_versions([project])
        if not rows:
            return None
        version = rows[0].version
        # read while LISTEN was up the whole time: later changes will be
        # notified, so it's safe to answer from memory from now on
        if self._listen and listening and generation == self._generation:
            self._remember(project, version)
        return version

    async def reconcile(self) -> None:
        for row in await self._fetch_versions(list(self._subscriptions)):
            self.publish(row.name, row.version)

    async def _fetch_versions(self, projects: list[str]) -> list:
        if not projects:
            return []
        async with self._db_engine.acquire() as conn:
            result = await conn.execute(
                select([Project.name, Project.version]).where(
                    Project.name.in_(projects)
                )
            )
            return await result.fetchall()

    async def run(self) -> None:
        if not self._listen:
            await self._poll()
            return

        delay = self._reconnect_delay
        while True:
            try:
                await self._listen_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Project version listener failed")
            self.listening.clear()
            self._versions.clear()
            self._generation += 1
            LISTENER_RECONNECTS_COUNTER.inc()
            # back off from scratch after a connection that held up, or a
            # long healthy run leaves the next outage at the maximum delay
            since, self._listening_since = self._listening_since, None
            if since is not None and time.monotonic() - since >= (
                self._stable_after
            ):
                delay = self._reconnect_delay
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_reconnect_delay)

    async def _listen_once(self) -> None:
        async with aiopg.connect(self._dsn, timeout=self._timeout) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(f"LISTEN {CHANNEL}")
            # Anything committed while we were not listening is only
            # visible in the table.
            await self.reconcile()
            self.listening.set()
            self._listening_since = time.monotonic()
            log.info("Listening for project version changes")

            while True:
                try:
                    notify = await asyncio.wait_for(
                        conn.notifies.get(), self._reconcile_interval
                    )
                except TimeoutError:
                    # Fails fast on a half-open connection, which would
                    # otherwise just never deliver anything again.
                    async with conn.cursor() as cursor:
                        await cursor.execute("SELECT 1")
                    await self.reconcile()
                    continue
                self._dispatch(notify.payload)

    def _dispatch(self, payload: str) -> None:
        try:
            data = json.loads(payload)
            project, version = data["project"], int(data["version"])
        except (ValueError, KeyError, TypeError):
            log.warning("Malformed project version notification: %r", payload)
            return
        self.publish(project, version)

    async def _poll(self) -> None:
        self.listening.set()
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self.reconcile()
            except Exception:
                log.exception("Project version poll failed")


async def init_project_version_hub(
    db_engine: aiopg.sa.Engine,
) -> AsyncGenerator[ProjectVersionHub, None]:
    hub = ProjectVersionHub(
        db_engine,
        config.postgres.dsn,
        listen=not config.readonly,
        reconcile_interval=config.http.sse_reconcile_interval,
        poll_interval=config.http.sse_replica_poll_interval,
        timeout=config.postgres.timeout,
    )
    task = asyncio.create_task(hub.run())
    try:
        yield hub
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
