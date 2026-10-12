from featureflags_client.http.state import HttpState
from featureflags_client.http.types import Flag


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
