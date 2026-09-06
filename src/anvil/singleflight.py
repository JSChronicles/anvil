"""Thread-safe cache that coalesces concurrent work for the same key."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar, cast

KeyT = TypeVar("KeyT")
ValueT = TypeVar("ValueT")

_MISSING = object()


@dataclass(slots=True)
class _SingleFlightEntry(Generic[ValueT]):
    """State shared by callers waiting for one in-flight computation."""

    event: threading.Event = field(default_factory=threading.Event)
    value: object = _MISSING
    error: BaseException | None = None


class SingleFlightCache(Generic[KeyT, ValueT]):
    """Cache completed values and share concurrent work for matching keys."""

    def __init__(self) -> None:
        self._values: dict[KeyT, ValueT] = {}
        self._flights: dict[KeyT, _SingleFlightEntry[ValueT]] = {}
        self._lock = threading.Lock()

    def get_or_create(
        self, *, key: KeyT, create: Callable[[], ValueT]
    ) -> tuple[ValueT, bool, bool]:
        """Return a cached value or create it once for concurrent callers.

        Args:
            key: Cache identity for the requested value.
            create: Callback used by the first caller when the key is absent.

        Returns:
            A tuple containing the value, whether it came from shared cache
            state, and whether this caller waited for an in-flight creator.

        Raises:
            BaseException: Re-raises any failure from the creator for the owner
                and every caller waiting on that flight.
        """

        with self._lock:
            if key in self._values:
                return self._values[key], True, False

            flight = self._flights.get(key)
            if flight is None:
                flight = _SingleFlightEntry[ValueT]()
                self._flights[key] = flight
                owns_create = True
            else:
                owns_create = False

        if owns_create:
            try:
                value = create()
            except BaseException as error:
                with self._lock:
                    flight.error = error
                    self._flights.pop(key, None)
                    flight.event.set()
                raise

            with self._lock:
                self._values[key] = value
                flight.value = value
                self._flights.pop(key, None)
                flight.event.set()
            return value, False, False

        flight.event.wait()
        if flight.error is not None:
            raise flight.error
        if flight.value is _MISSING:
            raise RuntimeError("Single-flight cache entry completed without a value")
        return cast("ValueT", flight.value), True, True
