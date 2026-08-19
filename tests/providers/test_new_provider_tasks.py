"""Focused behavior tests for the first Cloudflare, Datadog, GitLab, and PagerDuty tasks."""

from types import SimpleNamespace

import pytest

from anvil.actions import ActionRecorder
from anvil.providers.cloudflare.tasks import list_dns_records
from anvil.providers.datadog.tasks import list_monitors
from anvil.providers.gitlab.tasks import list_secret_scanning_alerts, search_code
from anvil.providers.pagerduty.tasks import list_services, remove_user


def _arguments(
    *, provider: str, target_type: str, session, metadata=None, dry_run=False
):
    return {
        "provider": provider,
        "execution_target_id": "123",
        "execution_target_name": "target",
        "execution_target_type": target_type,
        "region": "global",
        "session": session,
        "dry_run": dry_run,
        "metadata": metadata or {},
        "dependency_data": {},
        "actions": ActionRecorder(actions=[]),
    }


def test_cloudflare_list_dns_records_uses_zone_scope() -> None:
    operation = SimpleNamespace(
        list=lambda **kwargs: [SimpleNamespace(id="record-1", type="A")]
    )
    session = SimpleNamespace(
        client=SimpleNamespace(dns=SimpleNamespace(records=operation))
    )
    arguments = _arguments(provider="cloudflare", target_type="zone", session=session)

    result = list_dns_records.run(**arguments)

    assert result["record_count"] == 1
    assert result["records"] == [{"id": "record-1", "type": "A"}]
    assert "Listed 1" in arguments["actions"].actions[0]


def test_datadog_list_monitors_serializes_sdk_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datadog_api_client.v1.api import monitors_api

    class FakeMonitorsApi:
        def __init__(self, client: object) -> None:
            assert client == "datadog-client"

        def list_monitors(self):
            return [SimpleNamespace(id=1, name="availability")]

    monkeypatch.setattr(monitors_api, "MonitorsApi", FakeMonitorsApi)
    arguments = _arguments(
        provider="datadog",
        target_type="organization",
        session=SimpleNamespace(client="datadog-client"),
    )

    result = list_monitors.run(**arguments)

    assert result == {
        "monitor_count": 1,
        "monitors": [{"id": 1, "name": "availability"}],
    }


class _GitLabManager:
    def __init__(self, items: list[object]) -> None:
        self.items = items
        self.parameters: dict[str, object] = {}

    def list(self, **parameters):
        self.parameters = parameters
        return iter(self.items)


def test_gitlab_secret_alert_parity_filters_vulnerabilities() -> None:
    vulnerabilities = _GitLabManager([{"id": 1, "severity": "high"}])
    project = SimpleNamespace(vulnerabilities=vulnerabilities)
    projects = SimpleNamespace(get=lambda project_id: project)
    session = SimpleNamespace(client=SimpleNamespace(projects=projects))
    arguments = _arguments(
        provider="gitlab",
        target_type="project",
        session=session,
        metadata={"severity": "high"},
    )

    result = list_secret_scanning_alerts.run(**arguments)

    assert result["alert_count"] == 1
    assert vulnerabilities.parameters["report_type"] == "secret_detection"
    assert vulnerabilities.parameters["severity"] == "high"


def test_gitlab_search_code_requires_query_and_bounds_results() -> None:
    project = SimpleNamespace(
        search=lambda scope, query, **kwargs: iter([{"path": "a"}, {"path": "b"}])
    )
    session = SimpleNamespace(
        client=SimpleNamespace(projects=SimpleNamespace(get=lambda project_id: project))
    )
    arguments = _arguments(
        provider="gitlab",
        target_type="project",
        session=session,
        metadata={"query": "token", "max_results": 1},
    )

    result = search_code.run(**arguments)

    assert result == {"query": "token", "match_count": 1, "matches": [{"path": "a"}]}


class _PagerDutyClient:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def iter_all(self, resource: str):
        assert resource == "services"
        return iter([{"id": "service-1"}])

    def rdelete(self, path: str) -> None:
        self.deleted.append(path)


def test_pagerduty_inventory_and_remove_user_dry_run() -> None:
    client = _PagerDutyClient()
    session = SimpleNamespace(client=client)
    inventory_arguments = _arguments(
        provider="pagerduty", target_type="account", session=session
    )

    inventory = list_services.run(**inventory_arguments)
    mutation_arguments = _arguments(
        provider="pagerduty",
        target_type="account",
        session=session,
        metadata={"user_id": "USER1"},
        dry_run=True,
    )
    mutation = remove_user.run(**mutation_arguments)

    assert inventory == {"service_count": 1, "services": [{"id": "service-1"}]}
    assert mutation == {"user_id": "USER1", "planned": True, "deleted": False}
    assert client.deleted == []
    assert mutation_arguments["actions"].actions[0].startswith("(dry-run)")


def test_pagerduty_remove_user_executes_delete() -> None:
    client = _PagerDutyClient()
    arguments = _arguments(
        provider="pagerduty",
        target_type="account",
        session=SimpleNamespace(client=client),
        metadata={"user_id": "USER1"},
    )

    result = remove_user.run(**arguments)

    assert result["deleted"] is True
    assert client.deleted == ["users/USER1"]


def test_pagerduty_remove_user_requires_user_id() -> None:
    with pytest.raises(RuntimeError, match="metadata.user_id"):
        remove_user.run(
            **_arguments(
                provider="pagerduty",
                target_type="account",
                session=SimpleNamespace(client=_PagerDutyClient()),
            )
        )
