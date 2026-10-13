import json
from collections.abc import AsyncIterator

import anyio
from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Header
from prometheus_client import Counter
from sse_starlette import EventSourceResponse, ServerSentEvent

from featureflags.http.container import Container
from featureflags.http.repositories.flags import FlagsRepository
from featureflags.http.types import (
    PreloadFlagsRequest,
    PreloadFlagsResponse,
    SyncFlagsRequest,
    SyncFlagsResponse,
)
from featureflags.services.project_versions import ProjectVersionHub

router = APIRouter(prefix="/flags", tags=["flags"])


http_request_total_per_project = Counter(
    "http_request_total_per_project",
    "Total number of requests by handler and project.",
    labelnames=["handler", "project"],
)


@router.post("/load")
@inject
async def load_flags(
    request: PreloadFlagsRequest,
    flags_repo: FlagsRepository = Depends(Provide[Container.flags_repo]),
) -> PreloadFlagsResponse:
    """
    Init flags for project and load flags.
    """

    http_request_total_per_project.labels("/load", request.project).inc()
    return await flags_repo.load(request)


@router.post("/sync")
@inject
async def sync_flags(
    request: SyncFlagsRequest,
    flags_repo: FlagsRepository = Depends(Provide[Container.flags_repo]),
) -> SyncFlagsResponse:
    """
    Sync flags for project.
    """

    http_request_total_per_project.labels("/sync", request.project).inc()
    return await flags_repo.sync(request)


@router.get("/stream")
@inject
async def stream_flags(
    project: str,
    version: int = 0,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    hub: ProjectVersionHub = Depends(Provide[Container.version_hub]),
) -> EventSourceResponse:
    """
    Stream project versions newer than `version`.

    Events only carry the version, clients fetch the flags with /sync.
    The current version is sent right away if the client is behind.
    Event ids are versions, so plain EventSource clients resume via
    Last-Event-ID.
    """

    http_request_total_per_project.labels("/stream", project).inc()
    if last_event_id is not None and last_event_id.isdigit():
        version = max(version, int(last_event_id))

    # sse-starlette sets this when uvicorn starts shutting down; ending the
    # generator then completes the response cleanly, otherwise the stream is
    # cancelled and every open one logs "returned without completing response"
    shutdown = anyio.Event()

    async def events() -> AsyncIterator[ServerSentEvent]:
        async with hub.subscribe(project, version) as subscription:
            while (
                new_version := await subscription.wait(shutdown)
            ) is not None:
                yield ServerSentEvent(
                    event="version",
                    id=str(new_version),
                    data=json.dumps({"version": new_version}),
                )

    return EventSourceResponse(
        events(), shutdown_event=shutdown, shutdown_grace_period=1
    )
