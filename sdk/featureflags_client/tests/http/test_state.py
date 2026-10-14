import pytest

from featureflags_client.http import state as state_module
from featureflags_client.http.state import HttpState
from featureflags_client.http.types import (
    Check,
    CheckVariable,
    Flag,
    Operator,
    Value,
    ValueCondition,
    VariableType,
)


def static_flag(enabled: bool) -> Flag:
    return Flag(
        name="new_checkout", enabled=enabled, overridden=True, conditions=[]
    )


def make_state() -> HttpState:
    return HttpState(
        project="shop", variables=[], flags=["new_checkout"], values=[]
    )


def test_newer_version_replaces_state():
    state = make_state()
    state.update([static_flag(False)], [], version=3)
    state.update([static_flag(True)], [], version=5)

    assert state.version == 5
    assert state.get_flag("new_checkout")({}) is True


def test_older_version_does_not_replace_state():
    # A late response (slow request, lagging server) must not roll the
    # client back to an older configuration.
    state = make_state()
    state.update([static_flag(True)], [], version=5)
    state.update([static_flag(False)], [], version=3)

    assert state.version == 5
    assert state.get_flag("new_checkout")({}) is True


def regexp_value(name: str, pattern: str) -> Value:
    return Value(
        name=name,
        enabled=True,
        overridden=True,
        value_default="old",
        value_override="new",
        conditions=[
            ValueCondition(
                checks=[
                    Check(
                        operator=Operator.REGEXP,
                        variable=CheckVariable("user", VariableType.STRING),
                        value=pattern,
                    )
                ],
                value_override="matched",
            )
        ],
    )


def test_failed_update_keeps_the_whole_previous_state(monkeypatch):
    # flags, values and version change together or not at all: a half
    # applied update would run new flags with old values under the old
    # version, and the next sync would not repair it
    state = make_state()
    state.update([static_flag(False)], [], version=1)

    def broken(values):
        raise RuntimeError("cannot compile values")

    monkeypatch.setattr(state_module, "update_values_state", broken)
    with pytest.raises(RuntimeError):
        state.update([static_flag(True)], [], version=2)

    assert state.version == 1
    assert state.get_flag("new_checkout")({}) is False


def test_invalid_regexp_does_not_block_the_update(caplog):
    # a rule saved with a broken pattern (as "[") must not stop every other
    # change from reaching the client: that condition just never matches
    state = make_state()
    state.update(
        [static_flag(True)],
        [regexp_value("PROMPT", "["), regexp_value("MODEL", "^vip")],
        version=2,
    )

    assert state.version == 2
    assert state.get_flag("new_checkout")({}) is True
    assert state.get_value("PROMPT")({"user": "["}) == "new"
    assert state.get_value("MODEL")({"user": "vip-1"}) == "matched"
    assert "invalid regular expression" in caplog.text
