import asyncio

import pytest
from sqlalchemy import select

from featureflags.graph import actions
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

    assert response.version == 0
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
