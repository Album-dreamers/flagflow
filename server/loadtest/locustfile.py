"""
SSE fan-out load test.

Every user behaves like an SDK with stream=True: it preloads with
/flags/load, holds one /flags/stream connection and, on each version
event, fetches the flags with /sync. Receive times go to
$LOADTEST_OUT/recv-<pid>.csv when the test stops; report.py joins them
with publish.py's commit times.

LOADTEST_PRELOAD=0 / LOADTEST_SYNC=0 leave out /flags/load and /flags/sync
to measure the stream fan-out alone (the first event, sent on connect,
then only sets the starting version).
"""

import csv
import json
import os
import time

from locust import HttpUser, constant, events, task
from locust.exception import StopUser

PROJECT = os.environ.get("LOADTEST_PROJECT", "loadtest")
OUT = os.environ.get("LOADTEST_OUT", "loadtest-out")
FLAGS = [
    f"FLAG_{i}" for i in range(int(os.environ.get("LOADTEST_FLAGS", "20")))
]
PRELOAD = os.environ.get("LOADTEST_PRELOAD", "1") == "1"
SYNC = os.environ.get("LOADTEST_SYNC", "1") == "1"

# (user, version, received_at, synced_at), one list per locust process
received = []


# Not FastHttpUser: geventhttpclient's response.read(n) blocks until n bytes
# arrived, so its iter_lines holds back small SSE events (until 1024 bytes
# or the next keep-alives). requests yields each HTTP chunk as it comes.
class StreamUser(HttpUser):
    # pause before reconnecting when a stream ends or fails
    wait_time = constant(1)

    def on_start(self) -> None:
        self.version = None
        if PRELOAD:
            with self.client.post(
                "/flags/load",
                json={"project": PROJECT, "version": 0, "flags": FLAGS},
                catch_response=True,
            ) as response:
                if not response.ok:
                    # a client that can't start never counts as up to date
                    response.failure(f"status {response.status_code}")
                    raise StopUser()
                self.version = response.json()["version"]

    @task
    def stream(self) -> None:
        with self.client.get(
            f"/flags/stream?project={PROJECT}&version={self.version or 0}",
            name="/flags/stream",
            stream=True,
            catch_response=True,
            # the server sends a keep-alive every 15s
            timeout=(10, 60),
        ) as response:
            for line in response.iter_lines(
                chunk_size=None, decode_unicode=True
            ):
                if not line.startswith("data: "):
                    continue
                received_at = time.time()
                version = json.loads(line.removeprefix("data: "))["version"]
                if self.version is None:
                    self.version = version
                    continue
                if version <= self.version:
                    continue
                if SYNC and not self.sync(version):
                    # stays behind: the next event or reconnect retries
                    continue
                received.append((id(self), version, received_at, time.time()))
                self.version = version
        # stream closed by the server: the task runs again and reconnects

    def sync(self, version: int) -> bool:
        """Fetch the flags; only a response at `version` with them counts."""
        with self.client.post(
            "/flags/sync",
            json={
                "project": PROJECT,
                "version": self.version,
                "flags": FLAGS,
                "values": [],
            },
            catch_response=True,
        ) as response:
            if not response.ok:
                response.failure(f"status {response.status_code}")
                return False
            data = response.json()
            names = {flag["name"] for flag in data["flags"]}
            if data["version"] < version or not names >= set(FLAGS):
                response.failure(
                    f"version {data['version']} < {version} or flags missing"
                )
                return False
            return True


@events.test_stop.add_listener
def dump(**_kwargs) -> None:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"recv-{os.getpid()}.csv")
    with open(path, "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["user", "version", "received_at", "synced_at"])
        writer.writerows((f"{os.getpid()}-{u}", *rest) for u, *rest in received)
