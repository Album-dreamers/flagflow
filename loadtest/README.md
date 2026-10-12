# Load test

Locust users that behave like the Python SDK with `stream=True`: each one
holds a `/flags/stream` connection and, on every version event, fetches the
flags with `/flags/sync`. `publish.py` bumps the project version the way a
save does (one transaction with the `NOTIFY`) and records the commit time;
`report.py` joins both and reports latencies from the commit:

- `commit -> event`: until a client got the SSE event;
- `commit -> flags applied`: until it also fetched the new flags;
- `all users applied, per version`: until the last client had them.

## Running

Needs a database migrated with `python -m featureflags alembic upgrade head`,
locust in some virtualenv, and a server config with `debug: false` (with
debug on, SQL echo logging dominates every number).

```bash
export CONFIG_PATH=... PGUSER=... PGPASS=... SECRET=...
export LOCUST=/path/to/venv/bin/locust OUT=/tmp/loadtest-1000

# USERS [LOCUST_PROCESSES] [VERSIONS] [INTERVAL_SECONDS]
loadtest/run.sh 1000 2 10 3
```

`run.sh` starts one `featureflags http` process (or the checkout in
`SERVER_DIR`), samples its RSS/CPU into `server.csv`, spawns the users at
`RATE` per second, publishes once they are all connected, and prints the
report.

Modes:

- `LOADTEST_PRELOAD=0 LOADTEST_SYNC=0`: streams only, to measure fan-out
  and how many streams one instance holds.
- `LOADTEST_PRELOAD=0`: streams plus `/sync` on every event, the load a
  change puts on the server.
- default: every user also preloads with `/flags/load` when it starts.

## Gotchas

- `FastHttpUser` can't be used: geventhttpclient's `read(n)` waits for `n`
  bytes, so its `iter_lines` holds back small SSE events.
- Locust and the server compete for the same CPUs when run on one machine;
  latencies include the clients parsing every stream.
- A version that lands while clients are still syncing the previous one is
  coalesced (clients only get the latest), and counts as missing in the
  `deliveries` line; `users at the final version` is the one that matters.
