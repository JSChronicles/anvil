from __future__ import annotations

from dataclasses import dataclass

import pytest

from anvil.actions import ActionRecorder
from anvil.providers.github.tasks import (
    audit_branch_protection,
    audit_repo_security_settings,
    audit_rulesets,
    list_code_scanning_alerts,
    list_dependabot_alerts,
    list_secret_scanning_alerts,
)


class FakeRestClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def rest_get_json(
        self, path: str, *, params: dict[str, object] | None = None
    ) -> object:
        self.calls.append({"path": path, "params": dict(params or {})})
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response

    def rest_get_json_pages(
        self, path: str, *, params: dict[str, object], max_results: int
    ) -> list[dict[str, object]]:
        self.calls.append(
            {"path": path, "params": dict(params), "max_results": max_results}
        )
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        if not isinstance(response, list):
            raise AssertionError("paginated response must be a list")
        return response[:max_results]


@dataclass(frozen=True)
class FakeSession:
    client: FakeRestClient


def _run_task(
    task,
    *,
    client: FakeRestClient,
    execution_target_id: str = "octo-org/example",
    execution_target_name: str = "octo-org/example",
    execution_target_type: str = "repository",
    metadata: dict[str, object] | None = None,
    provider: str = "github",
) -> tuple[dict[str, object], list[str]]:
    actions = ActionRecorder(actions=[])
    result = task.run(
        provider=provider,
        execution_target_id=execution_target_id,
        execution_target_name=execution_target_name,
        execution_target_type=execution_target_type,
        region="global",
        session=FakeSession(client=client),
        dry_run=False,
        metadata={} if metadata is None else metadata,
        actions=actions,
    )
    return result, actions.actions


@pytest.mark.parametrize(
    ("task", "path", "metadata", "expected_filters"),
    [
        (
            list_code_scanning_alerts,
            "/repos/octo-org/example/code-scanning/alerts",
            {"state": "open", "severity": "high", "max_results": 1},
            {"state": "open", "severity": "high"},
        ),
        (
            list_secret_scanning_alerts,
            "/repos/octo-org/example/secret-scanning/alerts",
            {"state": "resolved", "validity": "active"},
            {"state": "resolved", "validity": "active"},
        ),
        (
            list_dependabot_alerts,
            "/repos/octo-org/example/dependabot/alerts",
            {"state": "open", "ecosystem": "pip"},
            {"state": "open", "ecosystem": "pip"},
        ),
    ],
)
def test_alert_tasks_list_repository_alerts_with_filters(
    task, path: str, metadata: dict[str, object], expected_filters: dict[str, object]
) -> None:
    client = FakeRestClient({path: [{"number": 1}, {"number": 2}]})

    result, actions = _run_task(task, client=client, metadata=metadata)

    assert result["alert_count"] == (1 if metadata.get("max_results") == 1 else 2)
    assert result["filters"] == expected_filters
    assert client.calls == [
        {
            "path": path,
            "params": expected_filters,
            "max_results": metadata.get("max_results", 100),
        }
    ]
    assert actions


@pytest.mark.parametrize(
    ("task", "path"),
    [
        (list_code_scanning_alerts, "/orgs/octo-org/code-scanning/alerts"),
        (list_secret_scanning_alerts, "/orgs/octo-org/secret-scanning/alerts"),
        (list_dependabot_alerts, "/orgs/octo-org/dependabot/alerts"),
    ],
)
def test_alert_tasks_support_organization_targets(task, path: str) -> None:
    client = FakeRestClient({path: [{"number": 7}]})

    result, _actions = _run_task(
        task,
        client=client,
        execution_target_id="octo-org",
        execution_target_name="octo-org",
        execution_target_type="organization",
    )

    assert result["alert_count"] == 1
    assert client.calls[0]["path"] == path


def test_audit_branch_protection_uses_default_branch() -> None:
    client = FakeRestClient(
        {
            "/repos/octo-org/example": {"default_branch": "main"},
            "/repos/octo-org/example/branches/main/protection": {
                "required_pull_request_reviews": {"required_approving_review_count": 2}
            },
        }
    )

    result, actions = _run_task(audit_branch_protection, client=client)

    assert result == {
        "branch": "main",
        "protected": True,
        "protection": {
            "required_pull_request_reviews": {"required_approving_review_count": 2}
        },
    }
    assert [call["path"] for call in client.calls] == [
        "/repos/octo-org/example",
        "/repos/octo-org/example/branches/main/protection",
    ]
    assert actions == [
        "Audited GitHub branch protection for repository octo-org/example "
        "branch main region global"
    ]


def test_audit_branch_protection_reports_unprotected_branch() -> None:
    client = FakeRestClient(
        {"/repos/octo-org/example/branches/release/protection": RuntimeError("404")}
    )

    result, _actions = _run_task(
        audit_branch_protection, client=client, metadata={"branch": "release"}
    )

    assert result == {"branch": "release", "protected": False, "protection": None}


def test_audit_rulesets_lists_repository_rulesets() -> None:
    path = "/repos/octo-org/example/rulesets"
    client = FakeRestClient({path: [{"id": 1, "name": "main"}]})

    result, _actions = _run_task(
        audit_rulesets, client=client, metadata={"includes_parents": False}
    )

    assert result == {
        "ruleset_count": 1,
        "rulesets": [{"id": 1, "name": "main"}],
        "includes_parents": False,
    }
    assert client.calls == [
        {"path": path, "params": {"includes_parents": False}, "max_results": 100}
    ]


def test_audit_repo_security_settings_returns_selected_fields() -> None:
    client = FakeRestClient(
        {
            "/repos/octo-org/example": {
                "full_name": "octo-org/example",
                "private": True,
                "default_branch": "main",
                "security_and_analysis": {
                    "advanced_security": {"status": "enabled"},
                    "secret_scanning": {"status": "enabled"},
                },
                "ignored": "value",
            }
        }
    )

    result, _actions = _run_task(audit_repo_security_settings, client=client)

    assert result == {
        "settings": {
            "full_name": "octo-org/example",
            "private": True,
            "default_branch": "main",
            "security_and_analysis": {
                "advanced_security": {"status": "enabled"},
                "secret_scanning": {"status": "enabled"},
            },
        }
    }


@pytest.mark.parametrize(
    "task", [audit_branch_protection, audit_rulesets, audit_repo_security_settings]
)
def test_repository_audit_tasks_require_repository_targets(task) -> None:
    with pytest.raises(RuntimeError, match="repository target"):
        _run_task(
            task,
            client=FakeRestClient({}),
            execution_target_id="octo-org",
            execution_target_name="octo-org",
            execution_target_type="organization",
        )
