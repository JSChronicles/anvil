from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from anvil.actions import ActionRecorder
from anvil.providers.github.tasks import search_code
from anvil.providers.github.tasks.search_code import run


@dataclass(frozen=True)
class FakeRepository:
    full_name: str


@dataclass(frozen=True)
class FakeTextMatch:
    object_url: str
    fragment: str
    matches: list[dict[str, object]]
    property: str


@dataclass(frozen=True)
class FakeSearchItem:
    repository: object
    path: str
    name: str
    sha: str
    url: str
    html_url: str
    score: float
    text_matches: list[object] = field(default_factory=list)


class FakeSearchResults:
    def __init__(
        self,
        items: list[object],
        *,
        total_count: int | None = None,
        incomplete_results: bool = False,
    ) -> None:
        self._items = items
        self.totalCount = len(items) if total_count is None else total_count
        self.incompleteResults = incomplete_results
        self.iterated_count = 0

    def __iter__(self):
        for item in self._items:
            self.iterated_count += 1
            yield item


class FakeGitHubClient:
    def __init__(
        self, *, results: object | None = None, error: Exception | None = None
    ):
        self.results = results or FakeSearchResults([])
        self.error = error
        self.calls: list[dict[str, object]] = []

    def search_code(self, query: str, *, highlight: bool = False) -> object:
        self.calls.append({"query": query, "highlight": highlight})
        if self.error is not None:
            raise self.error
        return self.results


@dataclass(frozen=True)
class FakeGitHubSession:
    client: FakeGitHubClient


def _run_task(
    *,
    client: FakeGitHubClient,
    execution_target_id: str = "octo-org/example",
    execution_target_name: str = "octo-org/example",
    execution_target_type: str = "repository",
    metadata: dict[str, object] | None = None,
    provider: str = "github",
) -> tuple[dict[str, object], list[str]]:
    actions = ActionRecorder(actions=[])
    result = run(
        provider=provider,
        execution_target_id=execution_target_id,
        execution_target_name=execution_target_name,
        execution_target_type=execution_target_type,
        region="global",
        session=FakeGitHubSession(client=client),
        dry_run=False,
        metadata={"query": "secret"} if metadata is None else metadata,
        dependency_data={},
        actions=actions,
    )
    return result, actions.actions


def _item(*, name: str = "settings.py") -> FakeSearchItem:
    return FakeSearchItem(
        repository=FakeRepository(full_name="octo-org/example"),
        path=f"src/{name}",
        name=name,
        sha="abc123",
        url=f"https://api.github.test/search/{name}",
        html_url=f"https://github.test/octo-org/example/blob/main/src/{name}",
        score=1.25,
        text_matches=[
            FakeTextMatch(
                object_url="https://api.github.test/match",
                fragment="TOKEN = '<em>secret</em>'",
                matches=[{"text": "secret", "indices": [9, 15]}],
                property="content",
            )
        ],
    )


def test_search_code_composes_repository_query_and_highlight() -> None:
    client = FakeGitHubClient(results=FakeSearchResults([_item()]))

    result, actions = _run_task(
        client=client,
        metadata={
            "query": "secret",
            "language": "Python",
            "path": "src",
            "extension": "py",
            "filename": "settings.py",
            "max_results": 2,
            "highlight": True,
        },
    )

    assert client.calls == [
        {
            "query": (
                "secret repo:octo-org/example language:Python "
                "path:src extension:py filename:settings.py"
            ),
            "highlight": True,
        }
    ]
    assert result["query"] == client.calls[0]["query"]
    assert result["returned_count"] == 1
    assert actions == [
        "Searched GitHub code for repository octo-org/example "
        "region global; returned 1 result(s)"
    ]


def test_search_code_composes_organization_query() -> None:
    client = FakeGitHubClient()

    _run_task(
        client=client,
        execution_target_id="octo-org",
        execution_target_name="octo-org",
        execution_target_type="organization",
    )

    assert client.calls == [{"query": "secret org:octo-org", "highlight": False}]


def test_search_code_normalizes_results_with_highlights() -> None:
    client = FakeGitHubClient(results=FakeSearchResults([_item()], total_count=17))

    result, _actions = _run_task(
        client=client, metadata={"query": "secret", "highlight": True}
    )

    assert result == {
        "query": "secret repo:octo-org/example",
        "total_count": 17,
        "incomplete_results": False,
        "returned_count": 1,
        "items": [
            {
                "repository": "octo-org/example",
                "path": "src/settings.py",
                "name": "settings.py",
                "sha": "abc123",
                "url": "https://api.github.test/search/settings.py",
                "html_url": (
                    "https://github.test/octo-org/example/blob/main/src/settings.py"
                ),
                "score": 1.25,
                "text_matches": [
                    {
                        "object_url": "https://api.github.test/match",
                        "fragment": "TOKEN = '<em>secret</em>'",
                        "matches": [{"text": "secret", "indices": [9, 15]}],
                        "property": "content",
                    }
                ],
            }
        ],
    }


def test_search_code_paginates_until_max_results() -> None:
    results = FakeSearchResults(
        [_item(name="a.py"), _item(name="b.py"), _item(name="c.py")]
    )
    client = FakeGitHubClient(results=results)

    result, _actions = _run_task(
        client=client, metadata={"query": "secret", "max_results": 2}
    )

    assert result["returned_count"] == 2
    assert [item["name"] for item in result["items"]] == ["a.py", "b.py"]
    assert results.iterated_count == 2


def test_search_code_reports_incomplete_results() -> None:
    client = FakeGitHubClient(
        results=FakeSearchResults([_item()], total_count=42, incomplete_results=True)
    )

    result, _actions = _run_task(client=client)

    assert result["total_count"] == 42
    assert result["incomplete_results"] is True


@pytest.mark.parametrize(
    ("metadata", "match"),
    [
        ({}, "metadata.query"),
        ({"query": ""}, "metadata.query"),
        ({"query": "secret", "max_results": 0}, "max_results"),
        ({"query": "secret", "max_results": True}, "max_results"),
        ({"query": "secret", "highlight": "yes"}, "highlight"),
    ],
)
def test_search_code_validates_metadata(
    metadata: dict[str, object], match: str
) -> None:
    with pytest.raises(RuntimeError, match=match):
        _run_task(client=FakeGitHubClient(), metadata=metadata)


def test_search_code_rejects_wrong_provider() -> None:
    with pytest.raises(RuntimeError, match="github provider"):
        _run_task(client=FakeGitHubClient(), provider="aws")


def test_search_code_rejects_wrong_target_type() -> None:
    with pytest.raises(RuntimeError, match="organization or repository"):
        _run_task(
            client=FakeGitHubClient(),
            execution_target_id="octo-org/team-a",
            execution_target_type="team",
        )


def test_search_code_requires_task_facing_search_session() -> None:
    actions = ActionRecorder(actions=[])

    with pytest.raises(RuntimeError, match="GitHub session with search_code"):
        run(
            provider="github",
            execution_target_id="octo-org/example",
            execution_target_name="octo-org/example",
            execution_target_type="repository",
            region="global",
            session=object(),
            dry_run=False,
            metadata={"query": "secret"},
            dependency_data={},
            actions=actions,
        )


@pytest.mark.parametrize(
    ("error", "match"),
    [
        (type("BadCredentialsException", (Exception,), {})("denied"), "authentication"),
        (
            type("RateLimitExceededException", (Exception,), {})("slow down"),
            "rate limit",
        ),
        (
            type(
                "GithubException",
                (Exception,),
                {"__module__": "github.GithubException"},
            )("bad request"),
            "API request",
        ),
    ],
)
def test_search_code_maps_pygithub_errors(error: Exception, match: str) -> None:
    client = FakeGitHubClient(error=error)

    with pytest.raises(RuntimeError, match=match):
        _run_task(client=client)


def test_search_code_module_does_not_import_pygithub() -> None:
    assert "github" not in search_code.__dict__
    assert callable(search_code.run)
