"""
Publish project versions the way a save does: bump the version and NOTIFY
in one transaction (actions.bump_project_versions), and record when each
one was committed.

Run with the server's config (CONFIG_PATH etc.):
    python loadtest/publish.py --count 20 --interval 2 --out loadtest-out
"""

import argparse
import asyncio
import csv
import os
import time

import aiopg.sa

from featureflags.config import config
from featureflags.graph.actions import bump_project_versions
from featureflags.models import Project


async def main(args: argparse.Namespace) -> None:
    os.makedirs(args.out, exist_ok=True)
    async with aiopg.sa.create_engine(dsn=config.postgres.dsn) as engine:
        rows = []
        for _ in range(args.count):
            async with engine.acquire() as conn:
                async with conn.begin():
                    await bump_project_versions(
                        Project.name == args.project, conn=conn
                    )
                committed_at = time.time()
                result = await conn.execute(
                    Project.__table__.select().where(
                        Project.name == args.project
                    )
                )
                version = (await result.first()).version
            rows.append((version, committed_at))
            print(f"published version {version}", flush=True)
            await asyncio.sleep(args.interval)

    with open(os.path.join(args.out, "published.csv"), "w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["version", "committed_at"])
        writer.writerows(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="loadtest")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--out", default="loadtest-out")
    asyncio.run(main(parser.parse_args()))
