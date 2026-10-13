import inspect
from collections.abc import Generator, Mapping
from enum import Enum, EnumMeta
from typing import Any, Union


def custom_asdict_factory(data: Any) -> dict:
    def convert_value(obj: Any) -> Any:
        if isinstance(obj, Enum):
            return obj.value
        return obj

    return {k: convert_value(v) for k, v in data}


def coerce_defaults(
    defaults: Union[EnumMeta, type, dict[str, bool]],
) -> dict[str, bool]:
    if isinstance(defaults, EnumMeta):  # deprecated
        defaults = {k: v.value for k, v in defaults.__members__.items()}  # type: ignore[var-annotated]
    elif inspect.isclass(defaults):
        defaults = {
            k: getattr(defaults, k)
            for k in dir(defaults)
            if k.isupper() and not k.startswith("_")
        }
    elif not isinstance(defaults, Mapping):
        raise TypeError(f"Invalid defaults type: {type(defaults)!r}")

    invalid = [
        k
        for k, v in defaults.items()
        if not isinstance(k, str) or not isinstance(v, bool)
    ]
    if invalid:
        raise TypeError(
            "Invalid flag definition: {}".format(", ".join(map(repr, invalid)))
        )

    return defaults


def coerce_values_defaults(
    defaults: Union[EnumMeta, type, dict[str, Union[int, str]]],
) -> dict[str, Union[int, str]]:
    if isinstance(defaults, EnumMeta):  # deprecated
        defaults = {k: v.value for k, v in defaults.__members__.items()}  # type: ignore[var-annotated]
    elif inspect.isclass(defaults):
        defaults = {
            k: getattr(defaults, k)
            for k in dir(defaults)
            if k.isupper() and not k.startswith("_")
        }
    elif not isinstance(defaults, Mapping):
        raise TypeError(f"Invalid defaults type: {type(defaults)!r}")

    invalid = [
        k
        for k, v in defaults.items()
        if not isinstance(k, str) or not (isinstance(v, (int, str)))
    ]
    if invalid:
        raise TypeError(
            "Invalid value definition: {}".format(", ".join(map(repr, invalid)))
        )

    return defaults


def intervals_gen(
    interval: int = 10,
    retry_interval_min: int = 1,
    retry_interval_max: int = 32,
) -> Generator[int, bool, None]:
    success = True
    retry_interval = retry_interval_min

    while True:
        if success:
            success = yield interval
            retry_interval = retry_interval_min
        else:
            success = yield retry_interval
            retry_interval = min(retry_interval * 2, retry_interval_max)


ROLLOUT_BUCKETS = 10_000


def fnv1a32(s: str) -> int:
    # Ported from growthbook-python (MIT), growthbook/core.py.
    hval = 0x811C9DC5
    if s.isascii():
        for b in s.encode():
            hval = ((hval ^ b) * 0x01000193) & 0xFFFFFFFF
    else:
        for ch in s:
            hval = ((hval ^ ord(ch)) * 0x01000193) & 0xFFFFFFFF
    return hval


def rollout_bucket(seed: str, value: Any) -> int:
    """
    Stable bucket in [0, ROLLOUT_BUCKETS) for a context value.

    Same as GrowthBook's gbhash v2. The seed is the flag or value name,
    so different flags on the same variable pick different users.
    """
    return fnv1a32(str(fnv1a32(f"{seed}{value}"))) % ROLLOUT_BUCKETS
