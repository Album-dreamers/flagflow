import pytest

from featureflags_client.http.utils import (
    ROLLOUT_BUCKETS,
    intervals_gen,
    rollout_bucket,
)


# gbhash v2 vectors from growthbook-python tests/cases.json ("hash" section)
@pytest.mark.parametrize(
    "seed, value, expected",
    [
        ("seed", "a", 0.0505),
        ("seed", "b", 0.2696),
        ("foo", "ab", 0.2575),
        ("foo", "def", 0.2019),
        ("89123klj", "8952klfjas09ujkasdf", 0.124),
        ("90850943850283058242805", "123", 0.7516),
        ("()**(%$##$%#$#", '___)((*":&', 0.0128),
    ],
)
def test_rollout_bucket_matches_growthbook(seed, value, expected):
    assert rollout_bucket(seed, value) / ROLLOUT_BUCKETS == expected


def test_intervals_gen_from_success():
    int_gen = intervals_gen()
    int_gen.send(None)

    assert int_gen.send(True) == 10
    assert int_gen.send(True) == 10
    assert int_gen.send(False) == 1
    assert int_gen.send(False) == 2
    assert int_gen.send(False) == 4
    assert int_gen.send(False) == 8
    assert int_gen.send(True) == 10
    assert int_gen.send(True) == 10
    assert int_gen.send(False) == 1
    assert int_gen.send(False) == 2
    assert int_gen.send(False) == 4
    assert int_gen.send(False) == 8
    assert int_gen.send(False) == 16
    assert int_gen.send(False) == 32
    assert int_gen.send(False) == 32
    assert int_gen.send(True) == 10
    assert int_gen.send(True) == 10


def test_intervals_gen_from_error():
    int_gen = intervals_gen()
    int_gen.send(None)

    assert int_gen.send(False) == 1
    assert int_gen.send(False) == 2
    assert int_gen.send(False) == 4
    assert int_gen.send(False) == 8
    assert int_gen.send(True) == 10
    assert int_gen.send(True) == 10
    assert int_gen.send(False) == 1
    assert int_gen.send(False) == 2
    assert int_gen.send(False) == 4
    assert int_gen.send(False) == 8
    assert int_gen.send(False) == 16
    assert int_gen.send(False) == 32
    assert int_gen.send(False) == 32
    assert int_gen.send(True) == 10
    assert int_gen.send(True) == 10
