"""
saveFlag / saveValue must apply the rule change, the version bumps and the
changelog entry together, and must reject a save made from a stale view.
"""

import asyncio

import pytest
from hiku.endpoint.graphql import AsyncBatchGraphQLEndpoint
from sqlalchemy import func, select

from featureflags.graph import actions, graph
from featureflags.graph.context import init_graph_context
from featureflags.models import Changelog, Flag, Operator, Project, Value
from featureflags.services import auth
from featureflags.tests.state import (
    mk_auth_user,
    mk_check,
    mk_condition,
    mk_flag,
    mk_project,
    mk_value,
    mk_variable,
)

SAVE_FLAG = """
    mutation SaveFlag($operations: [SaveFlagOperation!]!, $version: Int) {
        saveFlag(operations: $operations, version: $version) { errors }
    }
"""

SAVE_VALUE = """
    mutation SaveValue($operations: [SaveValueOperation!]!, $version: Int) {
        saveValue(operations: $operations, version: $version) { errors }
    }
"""


@pytest.fixture
def graphql(db_engine, graph_engine, ldap):
    endpoint = AsyncBatchGraphQLEndpoint(
        engine=graph_engine,
        query_graph=graph.GRAPH,
        mutation_graph=graph.MUTATION_GRAPH,
    )

    async def call(user, query, **variables):
        ctx = init_graph_context(
            session=auth.TestSession(user.id),
            engine=db_engine,
            ldap=ldap,
        )
        return await endpoint.dispatch(
            {"query": query, "variables": variables}, ctx
        )

    return call


def enable(flag):
    return [{"type": "enable_flag", "payload": {"flag_id": str(flag.id)}}]


def disable(flag):
    return [{"type": "disable_flag", "payload": {"flag_id": str(flag.id)}}]


def save_errors(result, mutation="saveFlag"):
    errors = result["data"][mutation]["errors"]
    return None if errors is None else list(errors)


async def scalar(conn, query):
    result = await conn.execute(query)
    return await result.scalar()


async def flag_state(conn, flag):
    result = await conn.execute(
        select([Flag.enabled, Flag.version]).where(Flag.id == flag.id)
    )
    row = await result.first()
    return row["enabled"], row["version"]


async def failing_step(**_kwargs):
    raise RuntimeError("simulated failure")


@pytest.mark.parametrize("step", ["postprocess", "update_changelog"])
async def test_failed_save_leaves_no_partial_change(
    step, monkeypatch, db_engine, conn, graphql
):
    # postprocess bumps the versions; update_changelog writes the audit
    # entry. If either fails, the flag change must not be visible: a flag
    # changed without a version bump is never synced to clients, and one
    # changed without a changelog entry is missing from the audit trail.
    user = await mk_auth_user(db_engine)
    project = await mk_project(db_engine, version=0)
    flag = await mk_flag(db_engine, enabled=False, project=project)
    monkeypatch.setattr(actions, step, failing_step)

    with pytest.raises(RuntimeError):
        await graphql(user, SAVE_FLAG, operations=enable(flag))

    enabled = await scalar(
        conn, select([Flag.enabled]).where(Flag.id == flag.id)
    )
    version = await scalar(
        conn, select([Project.version]).where(Project.id == project.id)
    )
    changelog = await scalar(
        conn,
        select([func.count()])
        .select_from(Changelog.__table__)
        .where(Changelog.flag == flag.id),
    )
    assert (enabled, version, changelog) == (False, 0, 0)


async def test_edit_from_stale_view_is_rejected(
    db_engine, conn, graph_engine, graphql
):
    # Two admins open the flag at version 0, while it has one 10%
    # condition, and both replace it: A with 20%, B with 30%. Before the
    # version check, B's new condition hit the (flag, position) unique
    # constraint, was dropped by ON CONFLICT DO NOTHING, and B was still
    # told it was saved, with a changelog entry for a change that never
    # happened.
    project = await mk_project(db_engine)
    variable = await mk_variable(db_engine, project=project)
    flag = await mk_flag(db_engine, enabled=True, project=project)
    old_check = await mk_check(
        db_engine,
        operator=Operator.PERCENT,
        variable=variable,
        value_string=None,
        value_number=10,
    )
    old_condition = await mk_condition(
        db_engine, flag=flag, checks=[old_check.id], project=project
    )

    def replace_condition(admin, percent):
        check = {"scope": admin, "value": "check"}
        return [
            {
                "type": "disable_condition",
                "payload": {"condition_id": str(old_condition.id)},
            },
            {
                "type": "add_check",
                "payload": {
                    "local_id": check,
                    "variable": str(variable.id),
                    "operator": Operator.PERCENT.value,
                    "kind": "value_number",
                    "value_number": percent,
                },
            },
            {
                "type": "add_condition",
                "payload": {
                    "flag_id": str(flag.id),
                    "local_id": {"scope": admin, "value": "condition"},
                    "checks": [{"local_id": check}],
                },
            },
        ]

    admin_a = await mk_auth_user(db_engine)
    admin_b = await mk_auth_user(db_engine)
    result_a = await graphql(
        admin_a, SAVE_FLAG, operations=replace_condition("a", 20), version=0
    )
    result_b = await graphql(
        admin_b, SAVE_FLAG, operations=replace_condition("b", 30), version=0
    )

    b_changelog = await scalar(
        conn,
        select([func.count()])
        .select_from(Changelog.__table__)
        .where(Changelog.auth_user == admin_b.id),
    )
    assert save_errors(result_a) is None
    assert save_errors(result_b), "B's stale save was reported as saved"
    assert b_changelog == 0


async def test_save_bumps_version_and_rejects_old_one(db_engine, conn, graphql):
    user = await mk_auth_user(db_engine)
    flag = await mk_flag(db_engine, enabled=False)

    # Clients that do not send a version keep working and still bump it.
    result = await graphql(user, SAVE_FLAG, operations=enable(flag))
    assert save_errors(result) is None
    assert await flag_state(conn, flag) == (True, 1)

    result = await graphql(user, SAVE_FLAG, operations=disable(flag), version=1)
    assert save_errors(result) is None
    assert await flag_state(conn, flag) == (False, 2)

    result = await graphql(user, SAVE_FLAG, operations=enable(flag), version=1)
    assert save_errors(result) == [
        "Flag was changed by someone else, reload and try again"
    ]
    assert await flag_state(conn, flag) == (False, 2)


async def test_concurrent_saves_from_same_version_one_wins(
    db_engine, conn, graphql
):
    # Both requests run at the same time on separate connections. The
    # second UPDATE waits for the first transaction's row lock, then
    # re-checks "version = 0" against the committed row and matches
    # nothing.
    admin_a = await mk_auth_user(db_engine)
    admin_b = await mk_auth_user(db_engine)
    flag = await mk_flag(db_engine, enabled=None)

    results = await asyncio.gather(
        graphql(admin_a, SAVE_FLAG, operations=enable(flag), version=0),
        graphql(admin_b, SAVE_FLAG, operations=disable(flag), version=0),
    )

    errors = [save_errors(result) for result in results]
    assert sorted(e is None for e in errors) == [False, True]
    winner_enabled = errors[0] is None
    assert await flag_state(conn, flag) == (winner_enabled, 1)


async def test_value_edit_from_stale_view_is_rejected(db_engine, conn, graphql):
    admin_a = await mk_auth_user(db_engine)
    admin_b = await mk_auth_user(db_engine)
    value = await mk_value(db_engine, value_override="prompt_v1")

    def set_override(override):
        return [
            {
                "type": "update_value_value_override",
                "payload": {
                    "value_id": str(value.id),
                    "value_override": override,
                },
            }
        ]

    result_a = await graphql(
        admin_a, SAVE_VALUE, operations=set_override("prompt_v2"), version=0
    )
    result_b = await graphql(
        admin_b, SAVE_VALUE, operations=set_override("prompt_v3"), version=0
    )

    override = await scalar(
        conn, select([Value.value_override]).where(Value.id == value.id)
    )
    assert save_errors(result_a, "saveValue") is None
    assert save_errors(result_b, "saveValue") == [
        "Value was changed by someone else, reload and try again"
    ]
    assert override == "prompt_v2"


async def test_version_is_readable_by_clients(db_engine, graphql):
    user = await mk_auth_user(db_engine)
    flag = await mk_flag(db_engine, enabled=False)
    value = await mk_value(db_engine)
    await graphql(user, SAVE_FLAG, operations=enable(flag))

    result = await graphql(
        user,
        """
        query ($flag: String!, $value: String!) {
            flag(id: $flag) { version }
            value(id: $value) { version }
        }
        """,
        flag=str(flag.id),
        value=str(value.id),
    )

    assert result["data"] == {
        "flag": {"version": 1},
        "value": {"version": 0},
    }
