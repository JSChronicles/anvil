from __future__ import annotations

import pytest

from anvil.providers.base import secret_fingerprint


@pytest.mark.parametrize("secret", [None, "", "   ", "\t\r\n"])
def test_secret_fingerprint_returns_none_for_missing_or_blank_values(
    secret: str | None,
) -> None:
    assert secret_fingerprint(secret) is None


def test_secret_fingerprint_preserves_trimmed_utf8_sha256_semantics() -> None:
    fingerprint = secret_fingerprint(" \t café \r\n")

    assert fingerprint == (
        "850f7dc43910ff890f8879c0ed26fe697c93a067ad93a7d50f466a7028a9bf4e"
    )
    assert len(fingerprint) == 64
    assert fingerprint == fingerprint.lower()
