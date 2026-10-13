import asyncio
import logging
import random
import time
from enum import EnumMeta
from typing import Any, Optional, Union

from featureflags_client.http.constants import Endpoints
from featureflags_client.http.managers.base import (
    AsyncBaseManager,
)
from featureflags_client.http.types import (
    Variable,
)
from featureflags_client.http.utils import intervals_gen

try:
    import httpx
    from httpx_sse import aconnect_sse
except ImportError:
    raise ImportError(
        "`httpx` is not installed, please install it to use HttpxManager "
        "like this `pip install 'featureflags-client[httpx]'`"
    ) from None

log = logging.getLogger(__name__)

# The server sends a keep-alive comment every 15s, a silent stream for
# longer than this is a dead connection.
STREAM_READ_TIMEOUT = 45

# Back off from scratch only after a stream that stayed up this long: a
# server accepting and dropping streams at once would otherwise be retried
# at the minimum interval forever (same idea as launchdarkly-eventsource's
# retry_delay_reset_threshold).
STREAM_STABLE_AFTER = 60


def jittered(interval: float) -> float:
    # After a server restart all clients reconnect at once; spreading the
    # delays over [interval / 2, interval] keeps them from doing it in lockstep.
    return interval * random.uniform(0.5, 1.0)  # noqa: S311 (not crypto)


class HttpxManager(AsyncBaseManager):
    """
    Feature flags manager for asyncio apps with `httpx` client.

    With `stream=True` the manager also listens to server-sent version
    events and syncs as soon as the project changes. Polling every
    `refresh_interval` seconds keeps running as a fallback.
    """

    def __init__(  # noqa: PLR0913
        self,
        url: str,
        project: str,
        variables: list[Variable],
        defaults: Union[EnumMeta, type, dict[str, bool]],
        values_defaults: Optional[
            Union[EnumMeta, type, dict[str, Union[int, str]]]
        ] = None,
        request_timeout: int = 5,
        refresh_interval: int = 10,
        stream: bool = False,
        snapshot_path: Optional[str] = None,
    ) -> None:
        super().__init__(
            url,
            project,
            variables,
            defaults,
            values_defaults,
            request_timeout,
            refresh_interval,
            snapshot_path,
        )
        self._session = httpx.AsyncClient(base_url=url)
        self._stream = stream
        self._stream_task: Optional[asyncio.Task] = None

    async def close(self) -> None:
        await self._session.aclose()

    async def _post(  # type: ignore
        self,
        url: Endpoints,
        payload: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        response = await self._session.post(
            url=httpx.URL(url.value),
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        response_data = response.json()
        return response_data

    def start(self) -> None:
        super().start()
        if self._stream:
            self._stream_task = asyncio.create_task(self._stream_loop())

    async def wait_closed(self) -> None:
        if self._stream_task is not None:
            self._stream_task.cancel()
            await asyncio.wait([self._stream_task])
        await super().wait_closed()

    async def _stream_loop(self) -> None:
        retry = intervals_gen()
        retry.send(None)

        while True:
            self._stream_connected_at: Optional[float] = None
            try:
                await self._consume_stream()
                log.info("Flags stream closed by server")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Flags stream failed: %r", exc)
            connected_at = self._stream_connected_at
            if (
                connected_at is not None
                and time.monotonic() - connected_at >= STREAM_STABLE_AFTER
            ):
                retry.send(True)
            interval = jittered(retry.send(False))
            log.info("Reconnecting to flags stream in %.1fs", interval)
            await asyncio.sleep(interval)

    async def _consume_stream(self) -> None:
        async with aconnect_sse(
            self._session,
            "GET",
            Endpoints.STREAM.value,
            # the server replies right away if this version is outdated,
            # so changes missed while disconnected are not lost
            params={
                "project": self._state.project,
                "version": self._state.version,
            },
            timeout=httpx.Timeout(
                self._request_timeout, read=STREAM_READ_TIMEOUT
            ),
        ) as event_source:
            event_source.response.raise_for_status()
            self._stream_connected_at = time.monotonic()
            log.info("Flags stream connected")

            async for event in event_source.aiter_sse():
                if event.event != "version":
                    continue
                version = event.json()["version"]
                if version > self._state.version:
                    log.debug("Flags stream: version %s, syncing", version)
                    await self.sync()
