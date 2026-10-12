"""
Percent rollout behaviour, checked through the public evaluation path
(flag_proc / value_proc), so the tests do not depend on how the hash is
implemented.
"""

from featureflags_client.http.conditions import flag_proc, value_proc
from featureflags_client.http.types import (
    Check,
    CheckVariable,
    Condition,
    Flag,
    Operator,
    Value,
    ValueCondition,
    VariableType,
)

USER_ID = "user.id"
USERS = [f"user-{i}" for i in range(10_000)]


def percent_check(percent: float) -> Check:
    return Check(
        operator=Operator.PERCENT,
        variable=CheckVariable(name=USER_ID, type=VariableType.STRING),
        value=percent,
    )


def percent_flag(name: str, percent: float) -> Flag:
    return Flag(
        name=name,
        enabled=True,
        overridden=True,
        conditions=[Condition(checks=[percent_check(percent)])],
    )


def percent_value(name: str, percent: float) -> Value:
    return Value(
        name=name,
        enabled=True,
        overridden=True,
        value_default="old",
        value_override="old",
        conditions=[
            ValueCondition(
                checks=[percent_check(percent)], value_override="new"
            )
        ],
    )


def users_in_flag(flag: Flag) -> set[str]:
    proc = flag_proc(flag)
    return {user for user in USERS if proc({USER_ID: user})}


def users_in_value(value: Value) -> set[str]:
    proc = value_proc(value)
    return {user for user in USERS if proc({USER_ID: user}) == "new"}


def test_flags_on_same_variable_pick_independent_users():
    # Two unrelated flags at 50% should share about 25% of users.
    # If the flag name is not part of the hash they share all 50%.
    checkout = users_in_flag(percent_flag("new_checkout", 50))
    search = users_in_flag(percent_flag("new_search", 50))

    overlap = len(checkout & search) / len(USERS)
    assert 0.20 <= overlap <= 0.30, overlap


def test_values_on_same_variable_pick_independent_users():
    prompt = users_in_value(percent_value("prompt_version", 50))
    model = users_in_value(percent_value("model_name", 50))

    overlap = len(prompt & model) / len(USERS)
    assert 0.20 <= overlap <= 0.30, overlap


def test_rollout_below_one_percent():
    # 0.5% of 10 000 users is about 50.
    selected = users_in_flag(percent_flag("risky_change", 0.5))

    assert 25 <= len(selected) <= 75, len(selected)


def test_rollout_distinguishes_fractional_percents():
    at_12 = users_in_flag(percent_flag("gradual", 12))
    at_12_5 = users_in_flag(percent_flag("gradual", 12.5))

    assert len(at_12_5) > len(at_12)


def test_increasing_percent_keeps_already_selected_users():
    previous: set[str] = set()
    for percent in [0.1, 1, 5, 10, 25, 50, 99.9, 100]:
        selected = users_in_flag(percent_flag("gradual", percent))
        assert previous <= selected, percent
        previous = selected


def test_rollout_matches_requested_share():
    for percent in [1, 10, 30, 70]:
        selected = users_in_flag(percent_flag("gradual", percent))
        share = len(selected) / len(USERS) * 100
        assert abs(share - percent) <= 1.5, (percent, share)
