from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator


class BenchmarkRecorder:
    """
    Small opt-in recorder for benchmark phase timings and metadata.
    """

    def __init__(
        self, *, enabled: bool = False, data: dict[str, object] | None = None
    ) -> None:
        self._data = data if data is not None else ({} if enabled else None)

    @property
    def enabled(self) -> bool:
        return self._data is not None

    @property
    def data(self) -> dict[str, object] | None:
        return self._data

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        if self._data is None:
            yield
            return

        started = time.perf_counter()
        try:
            yield
        finally:
            self._data[name] = time.perf_counter() - started

    def set(self, name: str, value: object) -> None:
        if self._data is not None:
            self._data[name] = value

    def pop(self, name: str) -> object | None:
        if self._data is None:
            return None

        return self._data.pop(name, None)

    def update(self, values: dict[str, object]) -> None:
        if self._data is not None:
            self._data.update(values)
