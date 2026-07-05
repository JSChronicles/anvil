from __future__ import annotations

from collections.abc import Mapping

DEFAULT_MAX_RESULTS = 100
DEFAULT_PER_PAGE = 100
REST_HEADERS = {"Accept": "application/vnd.github+json"}


def metadata_bool(
    *, task_name: str, metadata: dict[str, object], key: str, default: bool
) -> bool:
    """Read a boolean task metadata value."""

    value = metadata.get(key, default)
    if not isinstance(value, bool):
        raise RuntimeError(f"{task_name} metadata.{key} must be a boolean")
    return value


def metadata_int(
    *,
    task_name: str,
    metadata: dict[str, object],
    key: str,
    default: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    """Read a bounded integer task metadata value."""

    value = metadata.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise RuntimeError(
            f"{task_name} metadata.{key} must be an integer greater than or equal "
            f"to {minimum}"
        )
    if maximum is not None and value > maximum:
        raise RuntimeError(
            f"{task_name} metadata.{key} must be less than or equal to {maximum}"
        )
    return value


def metadata_string(
    *,
    task_name: str,
    metadata: dict[str, object],
    key: str,
    required: bool = False,
) -> str | None:
    """Read an optional or required string task metadata value."""

    value = metadata.get(key)
    if value is None:
        if required:
            raise RuntimeError(f"{task_name} requires metadata.{key} to be a string")
        return None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{task_name} metadata.{key} must be a non-empty string")
    return value.strip()


def metadata_params(
    *, task_name: str, metadata: dict[str, object], allowed_keys: tuple[str, ...]
) -> dict[str, object]:
    """Build GitHub REST query parameters from scalar metadata filters."""

    params: dict[str, object] = {}
    for key in allowed_keys:
        value = metadata.get(key)
        if value is None:
            continue
        if not isinstance(value, str | int | float | bool) or (
            isinstance(value, str) and not value.strip()
        ):
            raise RuntimeError(
                f"{task_name} metadata.{key} must be a string, number, or boolean"
            )
        params[key] = value.strip() if isinstance(value, str) else value
    return params


def require_github_provider(*, task_name: str, provider: str) -> None:
    """Validate that a task is running under the GitHub provider."""

    if provider != "github":
        raise RuntimeError(f"{task_name} requires the github provider")


def require_repository_target(
    *, task_name: str, execution_target_id: str, execution_target_type: str
) -> tuple[str, str]:
    """Validate and split a GitHub repository execution target."""

    if execution_target_type != "repository":
        raise RuntimeError(f"{task_name} requires a GitHub repository target")
    owner, separator, repo = execution_target_id.partition("/")
    if not owner or separator != "/" or not repo:
        raise RuntimeError(
            f"{task_name} requires execution_target_id to use owner/repo"
        )
    return owner, repo


def alert_endpoint(
    *, task_name: str, execution_target_id: str, execution_target_type: str, suffix: str
) -> str:
    """Return an organization or repository alert endpoint path."""

    if execution_target_type == "organization":
        return f"/orgs/{execution_target_id}/{suffix}"
    owner, repo = require_repository_target(
        task_name=task_name,
        execution_target_id=execution_target_id,
        execution_target_type=execution_target_type,
    )
    return f"/repos/{owner}/{repo}/{suffix}"


def list_rest_items(
    *,
    session: object,
    path: str,
    params: dict[str, object],
    max_results: int,
    per_page: int = DEFAULT_PER_PAGE,
) -> list[dict[str, object]]:
    """List paginated GitHub REST items as JSON-serializable mappings."""

    client = _session_client(session=session)
    custom = getattr(client, "rest_get_json_pages", None)
    if callable(custom):
        return [
            item
            for item in _jsonable(custom(path, params=params, max_results=max_results))
            if isinstance(item, dict)
        ][:max_results]

    items: list[dict[str, object]] = []
    page = 1
    while len(items) < max_results:
        request_params = dict(params)
        request_params["page"] = page
        request_params["per_page"] = min(per_page, max_results - len(items))
        data = rest_get(session=session, path=path, params=request_params)
        if not isinstance(data, list):
            raise RuntimeError(f"GitHub REST endpoint {path} did not return a list")

        page_items = [item for item in data if isinstance(item, dict)]
        items.extend(page_items)
        if len(data) < request_params["per_page"]:
            break
        page += 1

    return items[:max_results]


def rest_get(
    *, session: object, path: str, params: dict[str, object] | None = None
) -> object:
    """Run one GitHub REST GET request through the session client."""

    client = _session_client(session=session)
    custom = getattr(client, "rest_get_json", None)
    if callable(custom):
        return _jsonable(custom(path, params=params or {}))

    requester = _requester(client=client)
    try:
        _headers, data = requester.requestJsonAndCheck(
            "GET", path, parameters=params or {}, headers=REST_HEADERS
        )
    except TypeError:
        _headers, data = requester.requestJsonAndCheck(
            "GET", path, params or {}, REST_HEADERS
        )
    except Exception as error:
        raise runtime_error_from_provider_error(error) from error

    return _jsonable(data)


def runtime_error_from_provider_error(error: Exception) -> RuntimeError:
    """Map common PyGithub failures to task-facing runtime errors."""

    error_name = type(error).__name__
    message = str(error)
    if error_name in {"BadCredentialsException", "BadUserAgentException"}:
        return RuntimeError(f"GitHub authentication failed: {message}")
    if error_name in {"RateLimitExceededException", "RateLimitExceeded"}:
        return RuntimeError(f"GitHub rate limit exceeded: {message}")
    if error_name in {"GithubException", "GithubRetry"} or error.__class__.__module__.startswith(
        "github"
    ):
        return RuntimeError(f"GitHub API request failed: {message}")
    return RuntimeError(f"GitHub REST request failed: {message}")


def _session_client(*, session: object) -> object:
    client = getattr(session, "client", None)
    if client is None:
        raise RuntimeError("GitHub REST tasks require a GitHub session client")
    return client


def _requester(*, client: object) -> object:
    raw_client = getattr(client, "raw_client", client)
    requester = getattr(raw_client, "requester", None)
    if requester is None:
        requester = getattr(raw_client, "_Github__requester", None)
    if requester is None or not callable(getattr(requester, "requestJsonAndCheck", None)):
        raise RuntimeError(
            "GitHub REST tasks require a PyGithub requester or rest_get_json helper"
        )
    return requester


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())

    return str(value)
