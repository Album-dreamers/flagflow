"""
Join publish.py's commit times with the locust users' receive times.

    python loadtest/report.py loadtest-out [--users N]

Latencies are measured from the commit of a version:
- event:   until a client got the SSE event
- applied: until it also fetched the new flags with /sync
"""

import argparse
import csv
import glob
import gzip
import os
import statistics


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))
    return ordered[index]


def summary(name: str, values: list[float]) -> str:
    ms = [v * 1000 for v in values]
    return (
        f"{name:<28} n={len(ms):<7} p50={percentile(ms, 50):8.1f}  "
        f"p90={percentile(ms, 90):8.1f}  p99={percentile(ms, 99):8.1f}  "
        f"max={max(ms):8.1f}  mean={statistics.fmean(ms):8.1f} ms"
    )


def main(args: argparse.Namespace) -> None:
    with open(os.path.join(args.out, "published.csv")) as file:
        committed = {
            int(r["version"]): float(r["committed_at"])
            for r in csv.DictReader(file)
        }

    rows = []
    for path in glob.glob(os.path.join(args.out, "recv-*.csv*")):
        # the runs kept in results/ are gzipped
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt") as file:
            rows.extend(csv.DictReader(file))

    users = {r["user"] for r in rows}
    event, applied = [], []
    per_version: dict[int, list[float]] = {}
    per_version_event: dict[int, list[float]] = {}
    last_version: dict[str, int] = {}
    for r in rows:
        version = int(r["version"])
        if version not in committed:
            continue
        event.append(float(r["received_at"]) - committed[version])
        per_version_event.setdefault(version, []).append(event[-1])
        applied.append(float(r["synced_at"]) - committed[version])
        per_version.setdefault(version, []).append(
            float(r["synced_at"]) - committed[version]
        )
        last_version[r["user"]] = max(last_version.get(r["user"], 0), version)

    final = max(committed)
    expected_users = args.users or len(users)
    pairs = expected_users * len(committed)
    print(
        f"versions published: {len(committed)}, users seen: {len(users)}"
        f" (expected {expected_users})"
    )
    print(
        f"(user, version) deliveries: {len(event)} of {pairs}"
        f" ({len(event) / pairs:.2%}); coalesced versions count as missing"
    )
    print(
        f"users at the final version {final}: "
        f"{sum(v == final for v in last_version.values())} of {expected_users}"
    )
    if not event:
        return
    print(summary("commit -> event", event))
    print(summary("commit -> flags applied", applied))
    print(
        summary(
            "all users applied, per version",
            [max(v) for v in per_version.values()],
        )
    )
    if args.per_version:
        for version in sorted(per_version_event):
            print(
                summary(
                    f"  version {version} -> event", per_version_event[version]
                )
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument(
        "--users", type=int, help="number of users that should have connected"
    )
    parser.add_argument(
        "--per-version",
        action="store_true",
        help="also report commit -> event for each version",
    )
    main(parser.parse_args())
