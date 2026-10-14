import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from featureflags.graph import actions
from featureflags.http.db import REPORT_INTERVAL, prepare_flags_project
from featureflags.http.repositories import flags as flags_module
from featureflags.http.repositories.flags import FlagsRepository
from featureflags.http.types import PreloadFlagsRequest, SyncFlagsRequest
from featureflags.models import Flag, Project
from featureflags.tests.state import mk_auth_user, mk_flag, mk_project
from featureflags.utils import select_scalar


@pytest.fixture
def flags_repo(db_engine, graph_engine):
    return FlagsRepository(db_engine=db_engine, graph_engine=graph_engine)


@pytest.fixture
def readonly_flags_repo(db_engine, graph_engine):
    return FlagsRepository(
        db_engine=db_engine, graph_engine=graph_engine, readonly=True
    )


async def project_id_by_name(db_engine, name):
    async with db_engine.acquire() as conn:
        return await select_scalar(
            conn, select([Project.id]).where(Project.name == name)
        )


async def flag_id_by_name(db_engine, name):
    async with db_engine.acquire() as conn:
        return await select_scalar(
            conn, select([Flag.id]).where(Flag.name == name)
        )


@pytest.mark.asyncio
async def test_load_readonly_unknown_project(readonly_flags_repo, db_engine):
    request = PreloadFlagsRequest(
        project="unknown-project",
        version=0,
        flags=["NEW_FLAG"],
        values=[("NEW_VALUE", "default")],
    )

    response = await readonly_flags_repo.load(request)

    assert response.version == 0
    assert response.flags == []
    assert response.values == []
    assert await project_id_by_name(db_engine, "unknown-project") is None
    assert await flag_id_by_name(db_engine, "NEW_FLAG") is None


@pytest.mark.asyncio
async def test_load_readonly_existing_project(readonly_flags_repo, db_engine):
    project = await mk_project(db_engine, version=3)
    flag = await mk_flag(db_engine, project=project, enabled=True)

    request = PreloadFlagsRequest(
        project=project.name,
        version=0,
        flags=[flag.name, "NEW_FLAG"],
    )

    response = await readonly_flags_repo.load(request)

    assert response.version == 3
    assert [f.name for f in response.flags] == [flag.name]

    # the unknown flag was not registered
    assert await flag_id_by_name(db_engine, "NEW_FLAG") is None

    # the existing flag's report timestamp was not touched
    # (the column has an insert default, so compare with the initial value)
    async with db_engine.acquire() as conn:
        reported = await select_scalar(
            conn,
            select([Flag.reported_timestamp]).where(Flag.id == flag.id),
        )
    assert reported == flag.reported_timestamp


@pytest.mark.asyncio
async def test_load_registers_entities_when_not_readonly(flags_repo, db_engine):
    request = PreloadFlagsRequest(
        project="new-project",
        version=0,
        flags=["NEW_FLAG"],
        values=[("NEW_VALUE", "default")],
    )

    response = await flags_repo.load(request)

    # new projects take their first version from the version sequence
    assert response.version > 0
    assert [f.name for f in response.flags] == ["NEW_FLAG"]
    assert await project_id_by_name(db_engine, "new-project") is not None
    assert await flag_id_by_name(db_engine, "NEW_FLAG") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("readonly", [True, False])
async def test_sync_unknown_project(db_engine, graph_engine, readonly):
    repo = FlagsRepository(
        db_engine=db_engine, graph_engine=graph_engine, readonly=readonly
    )

    response = await repo.sync(
        SyncFlagsRequest(project="unknown-project", version=5)
    )

    assert response.version == 0
    assert response.flags == []
    assert response.values == []


@pytest.mark.asyncio
async def test_sync_empty_diff_until_version_bump(
    readonly_flags_repo, db_engine
):
    project = await mk_project(db_engine, version=0)
    flag = await mk_flag(db_engine, project=project, enabled=True)

    # client is at the current version: empty diff, even though the flag
    # row exists (registered-but-unconfigured flags have no effect)
    response = await readonly_flags_repo.sync(
        SyncFlagsRequest(project=project.name, version=0)
    )
    assert response.version == 0
    assert response.flags == []

    # a UI edit on the master bumps the version (replicated here)
    async with db_engine.acquire() as conn:
        await conn.execute(
            Project.__table__.update()
            .where(Project.id == project.id)
            .values({Project.version: 1})
        )

    response = await readonly_flags_repo.sync(
        SyncFlagsRequest(project=project.name, version=0)
    )
    assert response.version == 1
    assert [f.name for f in response.flags] == [flag.name]


@pytest.mark.asyncio
async def test_syncs_to_the_same_version_build_flags_once(
    flags_repo, db_engine, monkeypatch
):
    # after a change every client syncs at once; build the state once
    project = await mk_project(db_engine, version=2)
    flag = await mk_flag(db_engine, project=project, enabled=True)
    builds = []
    build = flags_module.exec_denormalize_graph

    async def counting_build(**kwargs):
        builds.append(kwargs["query"])
        return await build(**kwargs)

    monkeypatch.setattr(flags_module, "exec_denormalize_graph", counting_build)

    responses = await asyncio.gather(
        *(
            flags_repo.sync(SyncFlagsRequest(project=project.name, version=1))
            for _ in range(20)
        )
    )
    assert len(builds) == 1
    assert all([f.name for f in r.flags] == [flag.name] for r in responses)

    # the next version is built again, with the new data
    other = await mk_flag(db_engine, project=project, enabled=True)
    async with db_engine.acquire() as conn:
        await conn.execute(
            Project.__table__.update()
            .where(Project.id == project.id)
            .values({Project.version: 3})
        )
    response = await flags_repo.sync(
        SyncFlagsRequest(project=project.name, version=2)
    )
    assert len(builds) == 2
    assert sorted(f.name for f in response.flags) == sorted(
        [flag.name, other.name]
    )


DELETE_PROJECT = """
    mutation DeleteProject($id: String!) { deleteProject(id: $id) { error } }
"""


@pytest.mark.asyncio
async def test_recreated_project_starts_above_its_old_versions(
    flags_repo, db_engine, graphql
):
    # SDKs, snapshots and the SSE hub only accept versions above the one
    # they have; a project recreated from 0 would never reach them again
    request = PreloadFlagsRequest(project="recreated", version=0, flags=["F"])
    await flags_repo.load(request)
    project_id = await project_id_by_name(db_engine, "recreated")
    for _ in range(3):
        async with db_engine.acquire() as conn, conn.begin():
            await actions.bump_project_versions(
                Project.id == project_id, conn=conn
            )
    old_version = (await flags_repo.load(request)).version

    user = await mk_auth_user(db_engine)
    result = await graphql(user, DELETE_PROJECT, id=str(project_id))
    assert result["data"]["deleteProject"]["error"] is None
    assert await project_id_by_name(db_engine, "recreated") is None

    assert (await flags_repo.load(request)).version > old_version


class _FailOnUse:
    def __getattr__(self, name):
        raise RuntimeError("simulated failure")


@pytest.mark.asyncio
async def test_failed_project_delete_keeps_everything(
    db_engine, graphql, monkeypatch
):
    # deleteProject removes flags before values; failing in between must
    # not leave a project without its flags
    project = await mk_project(db_engine)
    flag = await mk_flag(db_engine, project=project)
    monkeypatch.setattr(actions, "Value", _FailOnUse())

    user = await mk_auth_user(db_engine)
    result = await graphql(user, DELETE_PROJECT, id=str(project.id))

    assert result["data"]["deleteProject"]["error"]
    assert await project_id_by_name(db_engine, project.name) == project.id
    assert await flag_id_by_name(db_engine, flag.name) == flag.id


async def reported_at(db_engine, flag):
    async with db_engine.acquire() as conn:
        return await select_scalar(
            conn,
            select([Flag.reported_timestamp]).where(Flag.id == flag.id),
        )


async def set_reported_at(db_engine, flag, timestamp):
    async with db_engine.acquire() as conn:
        await conn.execute(
            Flag.__table__.update()
            .where(Flag.id == flag.id)
            .values({Flag.reported_timestamp: timestamp})
        )


@pytest.mark.asyncio
async def test_load_refreshes_reported_timestamp_at_most_every_interval(
    flags_repo, db_engine
):
    project = await mk_project(db_engine)
    flag = await mk_flag(db_engine, project=project)
    request = PreloadFlagsRequest(
        project=project.name, version=0, flags=[flag.name]
    )

    stale = datetime.utcnow() - REPORT_INTERVAL - timedelta(seconds=5)
    await set_reported_at(db_engine, flag, stale)
    await flags_repo.load(request)
    refreshed = await reported_at(db_engine, flag)
    assert refreshed > stale

    # loaded again right away: no write
    await flags_repo.load(request)
    assert await reported_at(db_engine, flag) == refreshed


class CountingConnection:
    def __init__(self, conn):
        self.conn = conn
        self.statements = []

    async def execute(self, query, *args, **kwargs):
        self.statements.append(query)
        return await self.conn.execute(query, *args, **kwargs)


@pytest.mark.asyncio
async def test_registering_known_entities_takes_a_few_queries(db_engine):
    # one query per kind, however many flags: clients starting together
    # must not each run a query per flag
    flags = [f"FLAG_{i}" for i in range(20)]
    values = [(f"VALUE_{i}", "default") for i in range(5)]
    request = PreloadFlagsRequest(
        project="many-flags", version=0, flags=flags, values=values
    )
    async with db_engine.acquire() as conn:
        await prepare_flags_project(request, conn=conn)
        counting = CountingConnection(conn)
        await prepare_flags_project(request, conn=counting)

    # project, flags, flag timestamps, values, value timestamps
    assert len(counting.statements) == 5


@pytest.mark.asyncio
async def test_concurrent_loads_register_new_flags_once(flags_repo, db_engine):
    request = PreloadFlagsRequest(
        project="starting-together",
        version=0,
        flags=["NEW_A", "NEW_B"],
        values=[("NEW_VALUE", "1")],
    )

    await asyncio.gather(*(flags_repo.load(request) for _ in range(5)))

    async with db_engine.acquire() as conn:
        flag_count = await select_scalar(
            conn,
            select([func.count()])
            .select_from(Flag.__table__)
            .where(Flag.name.in_(["NEW_A", "NEW_B"])),
        )
    assert flag_count == 2


@pytest.mark.asyncio
async def test_new_flags_are_stamped_when_registered(flags_repo, db_engine):
    # the defaults were datetime.utcnow() called once at import: every new
    # flag got the server's start time as its creation time
    before = datetime.utcnow()
    await flags_repo.load(
        PreloadFlagsRequest(project="stamped", version=0, flags=["NEW"])
    )
    async with db_engine.acquire() as conn:
        created = await select_scalar(
            conn,
            select([Flag.created_timestamp]).where(Flag.name == "NEW"),
        )
    assert created >= before


@pytest.mark.asyncio
async def test_registering_a_value_moves_the_version(db_engine, graph_engine):
    # a new value changes what /load and /sync return (clients use its
    # server default), so caches on other instances and SDKs holding the
    # current version must be moved on, not keep serving the list without it
    instance_a = FlagsRepository(db_engine=db_engine, graph_engine=graph_engine)
    before = await instance_a.load(
        PreloadFlagsRequest(project="registering", version=0, flags=["BASE"])
    )

    request = PreloadFlagsRequest(
        project="registering",
        version=before.version,
        flags=["BASE"],
        values=[("PROMPT", "first-default")],
    )
    from_a = await instance_a.load(request)
    instance_b = FlagsRepository(db_engine=db_engine, graph_engine=graph_engine)
    from_b = await instance_b.load(request)

    assert from_a.version > before.version
    assert [v.name for v in from_a.values] == ["PROMPT"]
    assert from_b.version == from_a.version
    assert [v.name for v in from_b.values] == ["PROMPT"]
