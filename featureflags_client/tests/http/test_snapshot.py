import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from featureflags_client.http.client import FeatureFlagsClient
from featureflags_client.http.managers.httpx import HttpxManager
from featureflags_client.http.managers.requests import RequestsManager
from featureflags_client.http.snapshot import Snapshot
from featureflags_client.http.types import (
    Flag,
    PreloadFlagsResponse,
    SyncFlagsResponse,
    Variable,
)


class Defaults:
    TEST = False


@pytest.fixture
def response(condition):
    return PreloadFlagsResponse(
        version=3,
        flags=[
            Flag(
                name="TEST",
                enabled=True,
                overridden=True,
                conditions=[condition],
            ),
        ],
        values=[],
    )


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "flags.json")


def make_manager(manager_class, variable, path):
    manager = manager_class(
        url="http://flags.server.example",
        project="test",
        variables=[Variable(variable.name, variable.type)],
        defaults=Defaults,
        snapshot_path=path,
    )
    # no background sync in sync managers
    manager._next_sync = datetime.utcnow() + timedelta(hours=1)
    return manager


def server_down(*_args, **_kwargs):
    raise ConnectionError("flags server is down")


def test_round_trip(path, response):
    Snapshot(path, "test").save(response)

    assert Snapshot(path, "test").load() == response


def test_missing_corrupt_or_foreign_snapshot_is_ignored(path, response):
    assert Snapshot(path, "test").load() is None

    Snapshot(path, "other").save(response)
    assert Snapshot(path, "test").load() is None

    with open(path, "w") as file:
        file.write('{"project": "test", "vers')
    assert Snapshot(path, "test").load() is None


def test_failed_save_keeps_previous_snapshot(path, response, tmp_path):
    snapshot = Snapshot(path, "test")
    snapshot.save(response)

    # e.g. the process dies or the disk fills up halfway through the write
    with patch("json.dump", side_effect=OSError("No space left on device")):
        snapshot.save(PreloadFlagsResponse(version=4, flags=[], values=[]))

    assert snapshot.load() == response
    assert os.listdir(tmp_path) == ["flags.json"]  # no temp file left behind


def test_unwritable_location_does_not_raise(tmp_path, response):
    Snapshot(str(tmp_path / "missing" / "flags.json"), "test").save(response)


def test_preload_saves_snapshot(variable, path, response):
    manager = make_manager(RequestsManager, variable, path)

    with patch.object(manager, "_post", return_value=response.to_dict()):
        manager.preload()

    with open(path) as file:
        assert json.load(file)["version"] == 3


def test_sync_saves_only_new_versions(variable, path, response):
    manager = make_manager(RequestsManager, variable, path)
    with patch.object(manager, "_post", return_value=response.to_dict()):
        manager.preload()
    os.remove(path)

    unchanged = SyncFlagsResponse(version=3, flags=[], values=[])
    with patch.object(manager, "_post", return_value=unchanged.to_dict()):
        manager.sync()

    assert not os.path.exists(path)


def test_preload_falls_back_to_snapshot(variable, check, path, response):
    Snapshot(path, "test").save(response)
    manager = make_manager(RequestsManager, variable, path)
    client = FeatureFlagsClient(manager)

    with patch.object(manager, "_post", side_effect=server_down):
        client.preload()

    assert manager._state.version == 3
    with client.flags({variable.name: check.value}) as flags:
        assert flags.TEST is True


def test_preload_without_snapshot_still_fails(variable, path):
    manager = make_manager(RequestsManager, variable, path)

    with patch.object(manager, "_post", side_effect=server_down), pytest.raises(
        ConnectionError
    ):
        manager.preload()


@pytest.mark.asyncio
async def test_async_preload_falls_back_to_snapshot(
    variable, check, path, response
):
    Snapshot(path, "test").save(response)
    manager = make_manager(HttpxManager, variable, path)
    client = FeatureFlagsClient(manager)

    with patch.object(manager, "_post", side_effect=server_down):
        await client.preload_async()

    with client.flags({variable.name: check.value}) as flags:
        assert flags.TEST is True
    await manager.close()
