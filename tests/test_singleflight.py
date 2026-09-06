from __future__ import annotations

import threading

from anvil.singleflight import SingleFlightCache


def _join_threads(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.join(timeout=1.0)
        assert not thread.is_alive()


def test_single_flight_cache_shares_concurrent_work() -> None:
    cache = SingleFlightCache[str, str]()
    started = threading.Event()
    release = threading.Event()
    calls = 0
    results: list[tuple[str, bool, bool]] = []

    def create() -> str:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=1.0)
        return "value"

    def lookup() -> None:
        results.append(cache.get_or_create(key="shared", create=create))

    threads = [threading.Thread(target=lookup) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert started.wait(timeout=1.0)
    release.set()
    _join_threads(threads)

    assert calls == 1
    assert sorted(results) == [("value", False, False), ("value", True, True)]


def test_single_flight_cache_releases_waiters_after_error_and_allows_retry() -> None:
    cache = SingleFlightCache[str, str]()
    started = threading.Event()
    release = threading.Event()
    errors: list[str] = []

    def fail() -> str:
        started.set()
        assert release.wait(timeout=1.0)
        raise ValueError("discovery failed")

    def lookup() -> None:
        try:
            cache.get_or_create(key="shared", create=fail)
        except ValueError as error:
            errors.append(str(error))

    threads = [threading.Thread(target=lookup) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert started.wait(timeout=1.0)
    release.set()
    _join_threads(threads)

    assert errors == ["discovery failed", "discovery failed"]
    assert cache.get_or_create(key="shared", create=lambda: "recovered") == (
        "recovered",
        False,
        False,
    )


def test_single_flight_cache_distinguishes_completed_hits_from_waiters() -> None:
    cache = SingleFlightCache[str, str]()

    assert cache.get_or_create(key="shared", create=lambda: "value") == (
        "value",
        False,
        False,
    )
    assert cache.get_or_create(key="shared", create=lambda: "unexpected") == (
        "value",
        True,
        False,
    )


def test_single_flight_cache_can_cache_none() -> None:
    cache = SingleFlightCache[str, None]()
    calls = 0

    def create() -> None:
        nonlocal calls
        calls += 1

    assert cache.get_or_create(key="shared", create=create) == (None, False, False)
    assert cache.get_or_create(key="shared", create=create) == (None, True, False)
    assert calls == 1


def test_single_flight_cache_allows_different_keys_to_create_concurrently() -> None:
    cache = SingleFlightCache[str, str]()
    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()
    results: list[tuple[str, bool, bool]] = []

    def create(value: str, started: threading.Event) -> str:
        started.set()
        assert release.wait(timeout=1.0)
        return value

    threads = [
        threading.Thread(
            target=lambda: results.append(
                cache.get_or_create(
                    key="first", create=lambda: create("first", first_started)
                )
            )
        ),
        threading.Thread(
            target=lambda: results.append(
                cache.get_or_create(
                    key="second", create=lambda: create("second", second_started)
                )
            )
        ),
    ]
    for thread in threads:
        thread.start()
    assert first_started.wait(timeout=1.0)
    assert second_started.wait(timeout=1.0)
    release.set()
    _join_threads(threads)

    assert sorted(results) == [("first", False, False), ("second", False, False)]
