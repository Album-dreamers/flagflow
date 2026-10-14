"""
    This module defines client -> server feedback, which is used to
    notify server about new projects/variables/flags
"""
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from aiopg.sa import SAConnection
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from featureflags.graph.actions import bump_project_versions
from featureflags.http.types import PreloadFlagsRequest
from featureflags.models import (
    PROJECT_VERSION_SEQUENCE,
    Flag,
    Project,
    Value,
    Variable,
)

# "Last reported" is refreshed at most this often. Writing it on every load
# made clients starting together update the same rows one after another,
# holding their pool connections until the pool ran out.
REPORT_INTERVAL = timedelta(minutes=1)


async def _select_project(name: str, *, conn: SAConnection) -> UUID:
    result = await conn.execute(
        select([Project.id]).where(Project.name == name)
    )
    return await result.scalar()


async def _insert_project(name: str, *, conn: SAConnection) -> UUID:
    result = await conn.execute(
        insert(Project.__table__)
        .values(
            {
                Project.id: uuid4(),
                Project.name: name,
                # above any version a deleted project of this name had
                Project.version: func.nextval(PROJECT_VERSION_SEQUENCE),
            }
        )
        .on_conflict_do_nothing()
        .returning(Project.id)
    )
    return await result.scalar()


async def _get_or_create_project(name: str, *, conn: SAConnection) -> UUID:
    assert name
    id_ = await _select_project(name, conn=conn)
    if id_ is None:  # not in db
        id_ = await _insert_project(name, conn=conn)
        if id_ is None:  # conflicting insert
            id_ = await _select_project(name, conn=conn)
            assert id_ is not None  # must be in db
    return id_


async def _missing(
    model: Any, project: UUID, entries: dict[str, dict], *, conn: SAConnection
) -> dict[str, dict]:
    """The variables/flags/values in `entries` the project doesn't have yet."""
    if not entries:
        return {}
    result = await conn.execute(
        select([model.name]).where(
            and_(model.project == project, model.name.in_(list(entries)))
        )
    )
    existing = {row.name for row in await result.fetchall()}
    return {
        name: extra for name, extra in entries.items() if name not in existing
    }


async def _insert(
    model: Any, project: UUID, entries: dict[str, dict], *, conn: SAConnection
) -> bool:
    """Insert new rows, `entries` maps a name to its extra columns."""
    if not entries:
        return False
    # another client may register them at the same time
    result = await conn.execute(
        insert(model.__table__)
        .values(
            [
                {
                    model.id: uuid4(),
                    model.project: project,
                    model.name: name,
                    **extra,
                }
                for name, extra in entries.items()
            ]
        )
        .on_conflict_do_nothing()
        .returning(model.id)
    )
    return bool(await result.fetchall())


async def _report(
    model: Any, project: UUID, names: list[str], *, conn: SAConnection
) -> None:
    if not names:
        return
    now = datetime.utcnow()
    await conn.execute(
        model.__table__.update()
        .where(
            and_(
                model.project == project,
                model.name.in_(names),
                or_(
                    model.reported_timestamp.is_(None),
                    model.reported_timestamp < now - REPORT_INTERVAL,
                ),
            )
        )
        .values({model.reported_timestamp: now})
    )


async def prepare_flags_project(
    request: PreloadFlagsRequest, conn: SAConnection
) -> None:
    project = await _get_or_create_project(request.project, conn=conn)
    variables = {
        var.name: {Variable.type: var.type} for var in request.variables
    }
    flags: dict[str, dict] = {name: {} for name in request.flags}
    values = {
        name: {
            Value.value_default: str(default),
            Value.value_override: str(default),
        }
        for name, default in request.values
    }

    # one query per kind when everything is known, the usual case
    new_variables = await _missing(Variable, project, variables, conn=conn)
    new_flags = await _missing(Flag, project, flags, conn=conn)
    new_values = await _missing(Value, project, values, conn=conn)
    if new_variables or new_flags or new_values:
        async with conn.begin():
            await _insert(Variable, project, new_variables, conn=conn)
            added_flags = await _insert(Flag, project, new_flags, conn=conn)
            added_values = await _insert(Value, project, new_values, conn=conn)
            if added_flags or added_values:
                # /load and /sync return them now: a new version makes every
                # instance rebuild its cached state and every SDK sync
                await bump_project_versions(Project.id == project, conn=conn)

    await _report(Flag, project, request.flags, conn=conn)
    await _report(Value, project, list(values), conn=conn)
