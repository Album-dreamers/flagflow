import aiopg.sa
from async_lru import alru_cache
from hiku.builder import Q, build
from hiku.engine import Engine
from hiku.query import Node as QueryNode
from sqlalchemy import select

from featureflags.graph.graph import exec_denormalize_graph
from featureflags.http.db import prepare_flags_project
from featureflags.http.types import (
    Flag,
    PreloadFlagsRequest,
    PreloadFlagsResponse,
    SyncFlagsRequest,
    SyncFlagsResponse,
    Value,
)
from featureflags.models import Project
from featureflags.services.auth import user_session
from featureflags.utils import select_scalar


def load_data_query(project: str) -> QueryNode:
    return build(
        [
            Q.flags(project_name=project)[
                Q.id,
                Q.name,
                Q.enabled,
                Q.overridden,
                Q.conditions[
                    Q.id,
                    Q.checks[
                        Q.id,
                        Q.variable[
                            Q.id,
                            Q.name,
                            Q.type,
                        ],
                        Q.operator,
                        Q.value_string,
                        Q.value_number,
                        Q.value_timestamp,
                        Q.value_set,
                    ],
                ],
            ],
            Q.values(project_name=project)[
                Q.id,
                Q.name,
                Q.enabled,
                Q.overridden,
                Q.value_default,
                Q.value_override,
                Q.conditions[
                    Q.id,
                    Q.value_override,
                    Q.checks[
                        Q.id,
                        Q.variable[
                            Q.id,
                            Q.name,
                            Q.type,
                        ],
                        Q.operator,
                        Q.value_string,
                        Q.value_number,
                        Q.value_timestamp,
                        Q.value_set,
                    ],
                ],
            ],
        ]
    )


class FlagsRepository:
    def __init__(
        self,
        db_engine: aiopg.sa.Engine,
        graph_engine: Engine,
        readonly: bool = False,
    ) -> None:
        self._db_engine = db_engine
        self._graph_engine = graph_engine
        self._readonly = readonly
        # After a change every client syncs to the same version at once:
        # build its flags once (concurrent requests share the build).
        # Entries never go stale: a version is read before its flags, so an
        # entry can only hold newer data than its key. Registering a flag
        # doesn't bump the version and may be missing from an entry, which
        # clients treat the same (an unconfigured flag uses the default).
        self._project_state = alru_cache(maxsize=256)(self._load_project_state)

    async def _load_project_state(
        self, project: str, version: int
    ) -> tuple[list[Flag], list[Value]]:
        result = await exec_denormalize_graph(
            graph_engine=self._graph_engine,
            query=load_data_query(project),
            db_engine=self._db_engine,
            session=user_session.get(),
        )
        flags = [Flag(**flag) for flag in result["flags"]]
        values = [Value(**value) for value in result["values"]]
        return flags, values

    async def get_project_version(self, project: str) -> int | None:
        async with self._db_engine.acquire() as conn:
            return await select_scalar(
                conn,
                select([Project.version]).where(Project.name == project),
            )

    async def prepare_project(
        self,
        request: PreloadFlagsRequest,
    ) -> None:
        """
        Initialize project from request, create/update entities in the database.
        """

        async with self._db_engine.acquire() as conn:
            await prepare_flags_project(request, conn=conn)

    async def load(self, request: PreloadFlagsRequest) -> PreloadFlagsResponse:
        """
        Initialize project from request, create/update entities in the database
        and return available flags.

        In readonly mode registration is skipped: flags unknown to this
        instance are absent from the response and clients fall back to
        their in-code defaults.
        """

        if not self._readonly:
            await self.prepare_project(request)

        current_version = await self.get_project_version(request.project)
        if current_version is None:
            # Project is not in the database (e.g. not replicated yet).
            # Serve an empty but valid state instead of failing.
            return PreloadFlagsResponse(version=0)

        flags, values = await self._project_state(
            request.project, current_version
        )

        return PreloadFlagsResponse(
            flags=flags,
            values=values,
            version=current_version,
        )

    async def sync(self, request: SyncFlagsRequest) -> SyncFlagsResponse:
        """
        Return updated flags if project version
        is different from the requested one.
        """

        current_version = await self.get_project_version(request.project)
        if current_version is None:
            # Project is not in the database (e.g. not replicated yet).
            return SyncFlagsResponse(version=0)

        if request.version != current_version:
            flags, values = await self._project_state(
                request.project, current_version
            )
        else:
            # Don't load flags and values if version is the same.
            flags = []
            values = []

        return SyncFlagsResponse(
            flags=flags,
            values=values,
            version=current_version,
        )
