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


async def _register(
    model: Any,
    project: UUID,
    entries: dict[str, dict],
    *,
    conn: SAConnection,
) -> None:
    """
    Make sure the project has these variables/flags/values, `entries` maps
    a name to the extra columns for a new row. One query when they all exist.
    """
    if not entries:
        return
    result = await conn.execute(
        select([model.name]).where(
            and_(model.project == project, model.name.in_(list(entries)))
        )
    )
    existing = {row.name for row in await result.fetchall()}
    missing = [name for name in entries if name not in existing]
    if missing:
        # another client may register them at the same time
        await conn.execute(
            insert(model.__table__)
            .values(
                [
                    {
                        model.id: uuid4(),
                        model.project: project,
                        model.name: name,
                        **entries[name],
                    }
                    for name in missing
                ]
            )
            .on_conflict_do_nothing()
        )


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
    await _register(
        Variable,
        project,
        {var.name: {Variable.type: var.type} for var in request.variables},
        conn=conn,
    )

    await _register(
        Flag, project, {name: {} for name in request.flags}, conn=conn
    )
    await _report(Flag, project, request.flags, conn=conn)

    values = {
        name: {
            Value.value_default: str(default),
            Value.value_override: str(default),
        }
        for name, default in request.values
    }
    await _register(Value, project, values, conn=conn)
    await _report(Value, project, list(values), conn=conn)
