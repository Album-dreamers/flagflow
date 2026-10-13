#!/usr/bin/env bash
# One load test run against a single `featureflags http` process.
#
#   loadtest/run.sh USERS [PROCESSES] [VERSIONS] [INTERVAL]
#
# Needs CONFIG_PATH, PGUSER, PGPASS, SECRET for the server (debug off, or
# SQL echo dominates), LOCUST (path to the locust binary) and OUT (results
# directory). Writes OUT/{published,recv-*,server}.csv and prints report.py.
#
# FAULT: a command run after the first VERSIONS are published and that
# returns once things are back (e.g. restarting Postgres); VERSIONS more
# are published after it. FAULT_SECONDS: how long it may take (default 30).
set -euo pipefail

USERS=$1
PROCESSES=${2:-1}
VERSIONS=${3:-20}
INTERVAL=${4:-3}
RATE=${RATE:-50}
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."

rm -rf "$OUT" && mkdir -p "$OUT"
# SERVER_DIR: run the server from another checkout (e.g. to compare versions)
VENV="$PWD/.venv"
(cd "${SERVER_DIR:-.}" && exec "$VENV/bin/python" -m featureflags http) \
    > "$OUT/server.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
sleep 4

# server RSS (MB) and CPU (%) once a second
(
    echo "time,rss_mb,cpu"
    while kill -0 $SERVER 2>/dev/null; do
        read -r rss cpu < <(ps -o rss=,pcpu= -p $SERVER) || break
        echo "$(date +%s),$((rss / 1024)),$cpu"
        sleep 1
    done
) > "$OUT/server.csv" &

SPAWN=$(( USERS / RATE + 5 ))
PUBLISHING=$(( VERSIONS * ${INTERVAL%.*} ))
if [ -n "${FAULT:-}" ]; then
    PUBLISHING=$(( 2 * PUBLISHING + ${FAULT_SECONDS:-30} ))
fi
RUN=$(( SPAWN + PUBLISHING + 15 ))
LOADTEST_OUT="$OUT" "$LOCUST" -f loadtest/locustfile.py --headless \
    -u "$USERS" -r "$RATE" -t "${RUN}s" --processes "$PROCESSES" \
    -H "http://127.0.0.1:${HTTP_PORT:-8081}" --only-summary \
    > "$OUT/locust.log" 2>&1 &
LOCUST_PID=$!

sleep "$SPAWN"
# worker processes write their recv-*.csv after the master is gone
WORKERS=$(pgrep -P $LOCUST_PID || true)
publish() {
    PYTHONPATH=. .venv/bin/python loadtest/publish.py \
        --count "$VERSIONS" --interval "$INTERVAL" --out "$OUT" "$@" > /dev/null
}
publish
if [ -n "${FAULT:-}" ]; then
    echo "fault at $(date +%T): $FAULT"
    bash -c "$FAULT"
    echo "recovered at $(date +%T)"
    publish --append
fi
wait $LOCUST_PID || true
for pid in $WORKERS; do
    while kill -0 "$pid" 2> /dev/null; do sleep 1; done
done
# stop the server first so server.csv ends where the run does
kill $SERVER 2> /dev/null || true
wait $SERVER 2> /dev/null || true

"$(dirname "$LOCUST")/python" loadtest/report.py "$OUT" --users "$USERS"
awk -F, 'NR > 1 { if ($2 > rss) rss = $2; cpu += $3; n++ }
    END { printf "server: max RSS %d MB, mean CPU %.0f%%\n", rss, cpu / n }' \
    "$OUT/server.csv"
echo "listener reconnects: $(grep -c 'Project version listener failed' "$OUT/server.log" || true)"
grep -E "^(GET|POST) |Aggregated" "$OUT/locust.log" | head -4 || true
