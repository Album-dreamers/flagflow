# Load test results

Raw data behind the numbers in the root README. Recompute any of them with
`report.py` (from `server/`):

```bash
python loadtest/report.py loadtest/results/fanout-10000-after --users 10000
python loadtest/report.py loadtest/results/pg-restart-300 --users 300 --per-version
# server memory: highest RSS in MB
awk -F, 'NR > 1 && $2 > m { m = $2 } END { print m }' loadtest/results/fanout-10000-after/server.csv
```

## Machine

- Alibaba Cloud ECS, 4 vCPU (Intel Xeon Platinum, 2 cores x 2 threads,
  KVM), 7 GB RAM, Ubuntu 22.04, kernel 5.15.
- Locust, the server and PostgreSQL all on this one machine, so they share
  the CPUs; latencies include locust parsing every stream.
- One `featureflags http` process: Python 3.11.17, uvicorn 0.54 with uvloop
  0.19, FastAPI 0.143. Config: [`bench.yaml`](bench.yaml) (debug off, logs
  at warning, pool of 10 connections). The pool-40 runs add
  `postgres.pool_maxsize: 40`.
- PostgreSQL 16.15 on the host (not in Docker), locust 2.46.7.
- The project has 20 flags (`locustfile.py`).

## Runs

All with `loadtest/run.sh USERS PROCESSES VERSIONS INTERVAL` and the
environment shown.

| Directory | README row | Command |
|---|---|---|
| `fanout-1000` | fan-out, 1,000 | `LOADTEST_PRELOAD=0 LOADTEST_SYNC=0 RATE=100 run.sh 1000 2 10 2` |
| `fanout-2000` | fan-out, 2,000 | `LOADTEST_PRELOAD=0 LOADTEST_SYNC=0 RATE=100 run.sh 2000 2 10 2` |
| `fanout-5000` | fan-out, 5,000 | `LOADTEST_PRELOAD=0 LOADTEST_SYNC=0 RATE=200 run.sh 5000 3 10 3` |
| `fanout-10000-before` | fan-out, 10,000 before the fix | `LOADTEST_PRELOAD=0 LOADTEST_SYNC=0 RATE=200 run.sh 10000 3 10 3` |
| `fanout-10000-after` | fan-out, 10,000 after the fix | same as above |
| `sync-1000-no-cache` | sync after push, before the cache | `LOADTEST_PRELOAD=0 RATE=100 SERVER_DIR=<checkout before 1194bc3> run.sh 1000 2 5 10` |
| `sync-1000-cache` | sync after push, with the cache | `LOADTEST_PRELOAD=0 RATE=100 run.sh 1000 2 5 10` |
| `startup-pool10-before` | clients starting together, before the fix | `RATE=50 run.sh 1000 2 5 10` |
| `startup-pool40-before` | same, pool of 40 | same as above, pool 40 |
| `startup-pool10-after` | clients starting together, after the fix | `RATE=50 run.sh 1000 2 5 10` |
| `startup-pool40-after` | same, pool of 40 | same as above, pool 40 |
| `pg-restart-300` | PostgreSQL restart | `LOADTEST_PRELOAD=0 RATE=100 FAULT=fault_pg.sh run.sh 300 2 3 3` |
| `two-instances-1000` | two http instances | [`multi.sh`](two-instances-1000/multi.sh): 500 users on each, one publisher |

"Before" and "after" refer to these commits:

- fan-out: `cfdd533` (new streams take the version from memory);
- sync: `1194bc3` (flags built once per version);
- startup: `1f016a1` (batched registration, throttled timestamps).

## Files

- `published.csv`: each version and when it was committed.
- `recv-*.csv.gz`: one per locust process; each user's receive time of the
  event and the time it had the flags. A file with just the header is from
  the locust master, which runs no users.
- `server.csv`: the server's RSS (MB) and CPU (%) once a second.
- `locust.log`: locust's summary; gzipped where errors made it large.
- `server.log` is not kept (up to 70 MB for the failing runs).

## Notes

- `fanout-1000` and `fanout-2000` ran before `cfdd533`. That commit changes
  how a new stream gets its first version; once connected, delivery is the
  same code, and at these sizes every stream connected.
- The `startup-*-before` runs used a locustfile that dropped connection
  errors without counting them (fixed in `268dee7`). That can only have made
  the failure rate before the fix look lower than it was.
- In `startup-pool40-after`, 40 `/sync` calls (0.8%) hit keep-alive
  connections the server had just closed after 5 idle seconds; those users
  synced on the next event and all ended at the final version.
- In `pg-restart-300`, the 4th version (126) is committed by `fault_pg.sh`
  right after the restart, before the hub has reconnected: nobody receives
  its `NOTIFY`, it arrives through the hub's catch-up (p50 0.95 s with
  `--per-version`).
- `fault_pg.sh` and `multi.sh` are kept as run, with this machine's paths.
