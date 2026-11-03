"""Tests for bounded Redis lifecycle storage.

These tests use a fake Redis transport; they do not require Redis credentials.
"""
import json

from lifecycle_shards import SHARDS, LEGACY, shard_for, shard_key, read, update


def test_shard_is_deterministic_and_bounded():
    assert shard_for("provider-123") == shard_for("provider-123")
    assert 0 <= shard_for("provider-123") < SHARDS
    assert shard_key(0) != LEGACY


def test_update_writes_only_sharded_keys():
    calls = []
    def fake_redis(commands):
        calls.extend(commands)
        return [json.dumps({"new_count":1, "repeat_count":0, "tracked":1}) for _ in commands], None
    incoming = {
        "listing-a": {"name":"Shattershard","price":100000},
        "listing-b": {"name":"Astatine","price":20000},
    }
    totals, error, observed = update([], 123, fake_redis, lambda _: incoming)
    assert error is None
    assert observed == incoming
    assert totals["new_count"] >= 1
    assert calls and all(cmd[0] == "EVAL" for cmd in calls)
    assert all(cmd[3].startswith("wynnflip:v172:lifecycle:") for cmd in calls)
    assert all(cmd[3] != LEGACY for cmd in calls)


def test_read_merges_legacy_without_mutating_it():
    legacy = {"entries": {
        "listing-a": {"name":"Shattershard","last_seen":100,"scans_seen":3},
        "listing-b": {"name":"Astatine","last_seen":130,"scans_seen":4},
    }}
    sharded = {"entries": {
        "listing-a": {"name":"Shattershard","last_seen":160,"scans_seen":5},
    }}
    def fake_redis(commands):
        out = []
        for cmd in commands:
            assert cmd[0] == "GET"
            if cmd[1] == LEGACY:
                out.append(json.dumps(legacy))
            elif cmd[1] == shard_key(0):
                out.append(json.dumps(sharded))
            else:
                out.append(None)
        return out, None
    entries, error = read(fake_redis)
    assert error is None
    assert entries["listing-a"]["last_seen"] == 160
    assert entries["listing-b"]["scans_seen"] == 4


def test_no_stable_ids_returns_error_not_fake_listings():
    counts, error, observed = update([], 123, lambda _: ([], None), lambda _: {})
    assert counts is None
    assert error == "no stable identifiers"
    assert observed == {}


def test_partial_shard_failure_is_reported():
    def fake_redis(commands):
        return None, "Redis command failed: timeout"
    incoming = {"listing-a": {"name":"Example", "price":1000}}
    counts, error, observed = update([], 123, fake_redis, lambda _: incoming)
    assert counts is None
    assert "timeout" in error
    assert observed == incoming
