from __future__ import annotations

import pytest

from anvil.providers.base import secret_fingerprint


@pytest.mark.parametrize("secret", [None, "", "   ", "\t\r\n"])
def test_secret_fingerprint_returns_none_for_missing_or_blank_values(
    secret: str | None,
) -> None:
    assert secret_fingerprint(secret) is None


def test_secret_fingerprint_is_stable_for_equal_trimmed_utf8_values() -> None:
    fingerprint = secret_fingerprint(" \t café \r\n")

    assert fingerprint == secret_fingerprint("café")
    assert len(fingerprint) == 64
    assert fingerprint == fingerprint.lower()


def test_secret_fingerprint_distinguishes_different_values() -> None:
    assert secret_fingerprint("first-secret") != secret_fingerprint("second-secret")
