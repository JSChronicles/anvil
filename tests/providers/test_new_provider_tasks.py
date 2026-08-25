"""Focused behavior tests for the first Cloudflare, Datadog, GitLab, and PagerDuty tasks."""

from types import ModuleType, SimpleNamespace

import pytest

from anvil.actions import ActionRecorder
from anvil.providers.cloudflare.tasks import (
    list_account_member,
    list_dns_record,
    remove_account_member,
)
from anvil.providers.datadog.tasks import disable_user, list_monitor, list_user
from anvil.providers.github.tasks import list_member as github_list_member
from anvil.providers.github.tasks import list_team as github_list_team
from anvil.providers.github.tasks import list_team_member as github_list_team_member
from anvil.providers.github.tasks import remove_team as github_remove_team
from anvil.providers.github.tasks import remove_team_member as github_remove_team_member
from anvil.providers.gitlab.tasks import list_member as gitlab_list_member
from anvil.providers.gitlab.tasks import list_secret_scanning_alert, search_code
from anvil.providers.pagerduty.tasks import list_service, remove_user


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

    result = list_dns_record.run(**arguments)

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

        def list_monitors(self, *, page_size: int):
            assert page_size == 2
            return [SimpleNamespace(id=1, name="availability")]

    monkeypatch.setattr(monitors_api, "MonitorsApi", FakeMonitorsApi)
    arguments = _arguments(
        provider="datadog",
        target_type="organization",
        session=SimpleNamespace(client="datadog-client"),
        metadata={"max_results": 2},
    )

    result = list_monitor.run(**arguments)

    assert result == {
        "monitor_count": 1,
        "monitors": [{"id": 1, "name": "availability"}],
    }


def test_datadog_list_and_disable_user_use_array_selectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datadog_api_client.v2.api import users_api

    disabled: list[str] = []

    class FakeUsersApi:
        def __init__(self, client: object) -> None:
            pass

        def list_users(self, **kwargs):
            return SimpleNamespace(data=[SimpleNamespace(id="USER1")])

        def disable_user(self, user_id: str) -> None:
            disabled.append(user_id)

    monkeypatch.setattr(users_api, "UsersApi", FakeUsersApi)
    session = SimpleNamespace(client=object())
    listed = list_user.run(
        **_arguments(
            provider="datadog",
            target_type="organization",
            session=session,
            metadata={"users": ["USER1"]},
        )
    )
    removed = disable_user.run(
        **_arguments(
            provider="datadog",
            target_type="organization",
            session=session,
            metadata={"users": ["USER1"]},
        )
    )

    assert listed["user_count"] == 1
    assert removed["disabled_count"] == 1
    assert disabled == ["USER1"]


def test_user_selector_metadata_rejects_scalar() -> None:
    with pytest.raises(RuntimeError, match="metadata.users.*array"):
        remove_user.run(
            **_arguments(
                provider="pagerduty",
                target_type="account",
                session=SimpleNamespace(client=_PagerDutyClient()),
                metadata={"users": "USER1"},
            )
        )


class _CloudflareMembers:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    def list(self, **kwargs):
        return [SimpleNamespace(id="MEMBER1")]

    def delete(self, member_id: str, *, account_id: str) -> None:
        self.deleted.append((member_id, account_id))


def test_cloudflare_account_member_list_and_remove() -> None:
    members = _CloudflareMembers()
    session = SimpleNamespace(
        client=SimpleNamespace(accounts=SimpleNamespace(members=members))
    )
    listed = list_account_member.run(
        **_arguments(
            provider="cloudflare",
            target_type="account",
            session=session,
            metadata={"members": ["MEMBER1"]},
        )
    )
    removed = remove_account_member.run(
        **_arguments(
            provider="cloudflare",
            target_type="account",
            session=session,
            metadata={"members": ["MEMBER1"]},
        )
    )

    assert listed["member_count"] == 1
    assert removed["removed_count"] == 1
    assert members.deleted == [("MEMBER1", "123")]


class _GithubOrganization:
    def __init__(self) -> None:
        self.member = SimpleNamespace(id=1, login="octocat")
        self.removed_members: list[object] = []
        self.team = SimpleNamespace(
            id=7,
            slug="platform",
            delete=lambda: None,
            get_members=lambda: [self.member],
            remove_membership=self.removed_members.append,
        )

    def get_members(self):
        return [self.member]

    def get_teams(self):
        return [self.team]


class _CountingIterator:
    def __init__(self, items: list[object]) -> None:
        self._items = iter(items)
        self.consumed = 0

    def __iter__(self):
        return self

    def __next__(self):
        item = next(self._items)
        self.consumed += 1
        return item


@pytest.mark.parametrize(
    ("task", "operation_name", "result_key"),
    [
        (github_list_member, "get_members", "member_count"),
        (github_list_team, "get_teams", "team_count"),
    ],
)
def test_github_list_limit_stops_paginated_iteration(
    task: ModuleType, operation_name: str, result_key: str
) -> None:
    items = [
        SimpleNamespace(id=index, login=f"user-{index}", slug=f"team-{index}")
        for index in range(5)
    ]
    iterator = _CountingIterator(items)
    organization = SimpleNamespace(**{operation_name: lambda: iterator})
    session = SimpleNamespace(
        target_id="octo-org",
        client=SimpleNamespace(get_organization=lambda login: organization),
    )

    result = task.run(
        **_arguments(
            provider="github",
            target_type="organization",
            session=session,
            metadata={"max_results": 2},
        )
    )

    assert result[result_key] == 2
    assert iterator.consumed == 2


def test_github_member_list_and_team_remove_dry_run() -> None:
    organization = _GithubOrganization()
    session = SimpleNamespace(
        target_id="octo-org",
        client=SimpleNamespace(get_organization=lambda login: organization),
    )
    listed = github_list_member.run(
        **_arguments(
            provider="github",
            target_type="organization",
            session=session,
            metadata={"members": ["octocat"]},
        )
    )
    removed = github_remove_team.run(
        **_arguments(
            provider="github",
            target_type="organization",
            session=session,
            metadata={"teams": ["platform"]},
            dry_run=True,
        )
    )

    assert listed["member_count"] == 1
    assert removed["planned_count"] == 1


def test_github_team_member_list_and_remove_use_array_selectors() -> None:
    organization = _GithubOrganization()
    session = SimpleNamespace(
        target_id="octo-org",
        client=SimpleNamespace(get_organization=lambda login: organization),
    )
    metadata = {"teams": ["platform"], "members": ["octocat"]}

    listed = github_list_team_member.run(
        **_arguments(
            provider="github",
            target_type="organization",
            session=session,
            metadata=metadata,
        )
    )
    removed = github_remove_team_member.run(
        **_arguments(
            provider="github",
            target_type="organization",
            session=session,
            metadata=metadata,
        )
    )

    assert listed["membership_count"] == 1
    assert removed["removed_count"] == 1
    assert organization.removed_members == [organization.member]


def test_gitlab_member_list_uses_group_boundary() -> None:
    manager = _GitLabManager([SimpleNamespace(id=10, username="alice")])
    group = SimpleNamespace(members_all=manager)
    session = SimpleNamespace(
        client=SimpleNamespace(groups=SimpleNamespace(get=lambda group_id: group))
    )

    result = gitlab_list_member.run(
        **_arguments(
            provider="gitlab",
            target_type="group",
            session=session,
            metadata={"members": ["10"]},
        )
    )

    assert result["member_count"] == 1


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

    result = list_secret_scanning_alert.run(**arguments)

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

    inventory = list_service.run(**inventory_arguments)
    mutation_arguments = _arguments(
        provider="pagerduty",
        target_type="account",
        session=session,
        metadata={"users": ["USER1"]},
        dry_run=True,
    )
    mutation = remove_user.run(**mutation_arguments)

    assert inventory == {"service_count": 1, "services": [{"id": "service-1"}]}
    assert mutation == {
        "requested_count": 1,
        "planned_count": 1,
        "removed_count": 0,
        "failed_count": 0,
        "users": [{"id": "USER1", "status": "planned"}],
    }
    assert client.deleted == []
    assert mutation_arguments["actions"].actions[0].startswith("(dry-run)")


def test_pagerduty_remove_user_executes_delete() -> None:
    client = _PagerDutyClient()
    arguments = _arguments(
        provider="pagerduty",
        target_type="account",
        session=SimpleNamespace(client=client),
        metadata={"users": ["USER1"]},
    )

    result = remove_user.run(**arguments)

    assert result["removed_count"] == 1
    assert result["users"] == [{"id": "USER1", "status": "removed"}]
    assert client.deleted == ["users/USER1"]


def test_pagerduty_remove_user_requires_users() -> None:
    with pytest.raises(RuntimeError, match="metadata.users"):
        remove_user.run(
            **_arguments(
                provider="pagerduty",
                target_type="account",
                session=SimpleNamespace(client=_PagerDutyClient()),
            )
        )
