"""
Search GitHub code in the current organization or repository target.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

from anvil.actions import ActionRecorder

__LOGGER__ = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 100


def _metadata_string(
    *, metadata: dict[str, object], key: str, required: bool = False
) -> str | None:
    value = metadata.get(key)
    if value is None:
        if required:
            raise RuntimeError(f"search_code requires metadata.{key} to be a string")
        return None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"search_code metadata.{key} must be a non-empty string")
    return value.strip()


def _metadata_max_results(*, metadata: dict[str, object]) -> int:
    value = metadata.get("max_results", DEFAULT_MAX_RESULTS)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RuntimeError(
            "search_code metadata.max_results must be a positive integer"
        )
    return value


def _metadata_highlight(*, metadata: dict[str, object]) -> bool:
    value = metadata.get("highlight", False)
    if not isinstance(value, bool):
        raise RuntimeError("search_code metadata.highlight must be a boolean")
    return value


def _quote_qualifier_value(value: str) -> str:
    if any(character.isspace() for character in value):
        return f'"{value}"'
    return value


def _query(
    *, metadata: dict[str, object], execution_target_id: str, execution_target_type: str
) -> str:
    query = _metadata_string(metadata=metadata, key="query", required=True)
    qualifiers: list[str] = []

    if execution_target_type == "organization":
        qualifiers.append(f"org:{execution_target_id}")
    elif execution_target_type == "repository":
        qualifiers.append(f"repo:{execution_target_id}")
    else:
        raise RuntimeError(
            "search_code requires a GitHub organization or repository execution target"
        )

    for key, qualifier in (
        ("language", "language"),
        ("path", "path"),
        ("extension", "extension"),
        ("filename", "filename"),
    ):
        value = _metadata_string(metadata=metadata, key=key)
        if value is not None:
            qualifiers.append(f"{qualifier}:{_quote_qualifier_value(value)}")

    return " ".join([query or "", *qualifiers])


def _get_value(item: object, key: str) -> object:
    if isinstance(item, Mapping):
        return item.get(key)
    return getattr(item, key, None)


def _get_text_value(item: object, key: str) -> str | None:
    value = _get_value(item, key)
    return value if isinstance(value, str) else None


def _get_number_value(item: object, key: str) -> int | float | None:
    value = _get_value(item, key)
    return (
        value
        if isinstance(value, int | float) and not isinstance(value, bool)
        else None
    )


def _repository_full_name(item: object) -> str | None:
    repository = _get_value(item, "repository")
    if repository is None:
        return None
    return _get_text_value(repository, "full_name") or _get_text_value(
        repository, "name"
    )


def _normalize_text_match(match: object) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for source_key, result_key in (
        ("object_url", "object_url"),
        ("objectUrl", "object_url"),
        ("fragment", "fragment"),
        ("matches", "matches"),
        ("property", "property"),
    ):
        value = _get_value(match, source_key)
        if value is None:
            continue
        if isinstance(value, list):
            normalized[result_key] = [
                dict(item) if isinstance(item, Mapping) else item for item in value
            ]
        elif isinstance(value, str | int | float | bool):
            normalized[result_key] = value
    return normalized


def _normalize_text_matches(item: object) -> list[dict[str, object]]:
    raw_matches = _get_value(item, "text_matches")
    if raw_matches is None:
        raw_matches = _get_value(item, "textMatches")
    if not isinstance(raw_matches, Iterable) or isinstance(raw_matches, str | bytes):
        return []
    return [_normalize_text_match(match) for match in raw_matches]


def _normalize_item(item: object, *, include_highlights: bool) -> dict[str, object]:
    result: dict[str, object] = {
        "repository": _repository_full_name(item),
        "path": _get_text_value(item, "path"),
        "name": _get_text_value(item, "name"),
        "sha": _get_text_value(item, "sha"),
        "url": _get_text_value(item, "url"),
        "html_url": _get_text_value(item, "html_url"),
    }
    score = _get_number_value(item, "score")
    if score is not None:
        result["score"] = score
    if include_highlights:
        result["text_matches"] = _normalize_text_matches(item)
    return result


def _total_count(search_results: object) -> int | None:
    value = _get_value(search_results, "totalCount")
    if value is None:
        value = _get_value(search_results, "total_count")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _incomplete_results(search_results: object) -> bool | None:
    value = _get_value(search_results, "incompleteResults")
    if value is None:
        value = _get_value(search_results, "incomplete_results")
    return value if isinstance(value, bool) else None


def _runtime_error_from_provider_error(error: Exception) -> RuntimeError:
    error_name = type(error).__name__
    message = str(error)
    if error_name in {"BadCredentialsException", "BadUserAgentException"}:
        return RuntimeError(f"GitHub search_code authentication failed: {message}")
    if error_name in {"RateLimitExceededException", "RateLimitExceeded"}:
        return RuntimeError(f"GitHub search_code rate limit exceeded: {message}")
    if error_name in {
        "GithubException",
        "GithubRetry",
    } or error.__class__.__module__.startswith("github"):
        return RuntimeError(f"GitHub search_code API request failed: {message}")
    return RuntimeError(f"GitHub search_code failed: {message}")


def _search_client(session: object) -> object:
    client = getattr(session, "client", None)
    if client is None or not callable(getattr(client, "search_code", None)):
        raise RuntimeError("search_code requires a GitHub session with search_code()")
    return client


def run(
    *,
    provider: str,
    execution_target_id: str,
    execution_target_name: str,
    execution_target_type: str,
    region: str,
    location: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    actions: ActionRecorder,
) -> dict[str, object]:
    """Search code in the current GitHub organization or repository target."""

    if provider != "github":
        raise RuntimeError("search_code requires the github provider")

    query = _query(
        metadata=metadata,
        execution_target_id=execution_target_id,
        execution_target_type=execution_target_type,
    )
    max_results = _metadata_max_results(metadata=metadata)
    highlight = _metadata_highlight(metadata=metadata)

    try:
        search_results = _search_client(session).search_code(query, highlight=highlight)
        items = []
        result_iterator = iter(search_results)
        while len(items) < max_results:
            try:
                item = next(result_iterator)
            except StopIteration:
                break
            items.append(_normalize_item(item, include_highlights=highlight))
    except RuntimeError:
        raise
    except Exception as error:
        raise _runtime_error_from_provider_error(error) from error

    result: dict[str, object] = {
        "query": query,
        "total_count": _total_count(search_results),
        "incomplete_results": _incomplete_results(search_results),
        "returned_count": len(items),
        "items": items,
    }

    __LOGGER__.info(
        f"Searched GitHub code for {execution_target_type} {execution_target_name} "
        f"location={location or region}; returned {len(items)} result(s)"
    )
    actions.record(
        f"Searched GitHub code for {execution_target_type} {execution_target_id} "
        f"location {location or region}; returned {len(items)} result(s)"
    )

    return result
