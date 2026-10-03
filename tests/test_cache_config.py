import pytest

from config import ConfigError, load_config
from utils.cache import CooldownTracker, TTLCache


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_ttl_cache_expires_and_bounds():
    clock = Clock()
    cache = TTLCache(max_size=3, ttl=10, clock=clock)
    for key in "abcd":
        cache.set(key, key.upper())
    assert len(cache) == 3
    assert cache.get("a") is None
    assert cache.get("d") == "D"
    clock.now = 11
    assert cache.get("d") is None


def test_cooldown_tracker_blocks_then_allows_and_stays_bounded():
    clock = Clock()
    tracker = CooldownTracker(5, max_size=3, clock=clock)
    assert tracker.hit("u") == 0
    assert tracker.hit("u") == pytest.approx(5)
    clock.now = 5.1
    assert tracker.hit("u") == 0
    for n in range(10):
        tracker.hit(f"user{n}")
    assert len(tracker) <= 3


def valid_env():
    return {
        "DISCORD_TOKEN": "a" * 60,
        "OWNER_ID": "42",
        "LAVALINK_HOST": "127.0.0.1",
        "LAVALINK_PASSWORD": "pw-secret",
    }


def test_config_defaults_and_secret_hiding():
    cfg = load_config(env=valid_env())
    assert cfg.default_search_source == "ytsearch"
    assert cfg.fallback_search_source == "scsearch"
    assert cfg.sync_on_start is False
    assert cfg.idle_timeout == 300 and cfg.alone_timeout == 60
    assert "a" * 60 not in repr(cfg)
    assert "pw-secret" not in repr(cfg)
    assert "pw-secret" in cfg.secrets()


def test_config_reports_all_problems_together():
    with pytest.raises(ConfigError) as info:
        load_config(env={"MAX_QUEUE_SIZE": "abc"})
    message = str(info.value)
    assert "DISCORD_TOKEN" in message and "OWNER_ID" in message
    assert "LAVALINK_PASSWORD" in message and "MAX_QUEUE_SIZE" in message


def test_config_extra_nodes_and_validation():
    env = valid_env()
    env["LAVALINK_EXTRA_NODES"] = '[{"name":"b","host":"10.0.0.2","port":2334,"password":"x"}]'
    cfg = load_config(env=env)
    assert [n.name for n in cfg.nodes] == ["main", "b"]
    env["LAVALINK_EXTRA_NODES"] = "not json"
    with pytest.raises(ConfigError):
        load_config(env=env)


def test_config_rejects_bad_search_source():
    env = valid_env()
    env["DEFAULT_SEARCH_SOURCE"] = "yt search!"
    with pytest.raises(ConfigError):
        load_config(env=env)
