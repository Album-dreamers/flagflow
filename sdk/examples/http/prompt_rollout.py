"""
Gradual rollout of a new prompt and model, driven from the admin UI.

LLM_VARIANT is a feature value: the app maps its name to a prompt template
and a model, so the two always change together. Two separate values would
not: a percent split is seeded by the value name, so "prompt v2 for 10%"
and "large model for 10%" would select different users.

Start the server (see the root README), run this script, then in the admin
UI open project ``llm-demo``, tab "values", LLM_VARIANT:

1. add a condition ``user.id percent 10`` with value ``v2``, switch the
   value on and apply: about 10% of users move to v2 within a second;
2. change the percent to 50 and apply: users already on v2 stay there,
   a rollout only adds users;
3. switch LLM_VARIANT off and apply: everyone is back on v1.

    # from the repository root
    uv run --no-project --with "./sdk[httpx]" \
        sdk/examples/http/prompt_rollout.py
"""

import asyncio
import contextlib
import logging
import os
from collections import Counter

from featureflags_client.http.client import FeatureFlagsClient
from featureflags_client.http.managers.httpx import HttpxManager
from featureflags_client.http.types import Variable, VariableType

log = logging.getLogger(__name__)

FF_URL = os.environ.get("FEATUREFLAGS_URL", "http://localhost:8081")
FF_PROJECT = "llm-demo"

# the UI offers "percent" only for number variables
USER_ID = Variable("user.id", VariableType.NUMBER)

VARIANTS = {
    "v1": {
        "model": "small-model",
        "prompt": "Answer the question: {question}",
    },
    "v2": {
        "model": "large-model",
        "prompt": "Think step by step, then answer briefly: {question}",
    },
}


class Values:
    LLM_VARIANT = "v1"


_unknown: set[str] = set()


def pick_variant(client: FeatureFlagsClient, user_id: int) -> str:
    with client.values({USER_ID.name: user_id}) as values:
        name = values.LLM_VARIANT
    if name not in VARIANTS:
        # a value is free text in the UI: a typo must not break requests
        if name not in _unknown:
            _unknown.add(name)
            log.warning("Unknown LLM_VARIANT %r, serving v1", name)
        return Values.LLM_VARIANT
    return name


def build_request(
    client: FeatureFlagsClient, user_id: int, question: str
) -> dict[str, str]:
    """What the app would send to the LLM API for this user."""
    variant = VARIANTS[pick_variant(client, user_id)]
    return {
        "model": variant["model"],
        "prompt": variant["prompt"].format(question=question),
    }


async def main() -> None:
    manager = HttpxManager(
        url=FF_URL,
        project=FF_PROJECT,
        variables=[USER_ID],
        defaults={},
        values_defaults=Values,
        # changes are pushed over SSE, polling stays as a fallback
        stream=True,
    )
    client = FeatureFlagsClient(manager)
    await client.preload_async()  # also registers the project and value
    manager.start()

    users = range(1, 2001)
    previous: dict[int, str] = {}
    try:
        while True:
            current = {user: pick_variant(client, user) for user in users}
            if current != previous:
                split = Counter(current.values())
                line = ", ".join(
                    f"{name}: {split[name] / len(users):.1%}"
                    for name in sorted(split)
                )
                was_v2 = [u for u, name in previous.items() if name == "v2"]
                if was_v2:
                    kept = sum(current[u] == "v2" for u in was_v2)
                    line += f" (kept v2: {kept} of {len(was_v2)})"
                request = build_request(client, 1, "2+2?")
                print(line, "| user 1 ->", request, flush=True)  # noqa: T201
                previous = current
            await asyncio.sleep(0.2)
    finally:
        await manager.wait_closed()
        await manager.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
