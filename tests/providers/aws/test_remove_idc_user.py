"""Behavior tests for the IAM Identity Center user removal task."""

from types import SimpleNamespace

import pytest

from anvil.actions import ActionRecorder
from anvil.providers.aws.tasks import remove_idc_user


USERS = [
    {
        "UserId": "user-1",
        "UserName": "alice",
        "Emails": [{"Value": "alice@example.com"}],
        "UserStatus": "ENABLED",
    },
    {
        "UserId": "user-2",
        "UserName": "bob",
        "Emails": [{"Value": "bob@example.com"}],
        "UserStatus": "DISABLED",
    },
    {
        "UserId": "user-3",
        "UserName": "carol",
        "Emails": [{"Value": "carol@example.com"}],
        "UserStatus": "DISABLED",
    },
]


class _Paginator:
    def paginate(self, **kwargs):
        assert kwargs == {"IdentityStoreId": "store-1"}
        return [{"Users": USERS}]


class _IdentityStoreClient:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def get_paginator(self, name: str):
        assert name == "list_users"
        return _Paginator()

    def delete_user(self, **kwargs) -> None:
        assert kwargs["IdentityStoreId"] == "store-1"
        self.deleted.append(kwargs["UserId"])


class _SsoAdminClient:
    def list_instances(self):
        return {
            "Instances": [
                {
                    "InstanceArn": "instance-arn",
                    "IdentityStoreId": "store-1",
                    "OwnerAccountId": "123",
                    "Status": "ACTIVE",
                }
            ]
        }


def _arguments(*, metadata: dict[str, object], dry_run: bool = True):
    identitystore = _IdentityStoreClient()
    clients = {"sso-admin": _SsoAdminClient(), "identitystore": identitystore}
    session = SimpleNamespace(client=lambda service, **kwargs: clients[service])
    actions = ActionRecorder(actions=[])
    return (
        {
            "provider": "aws",
            "execution_target_id": "123",
            "execution_target_name": "owner",
            "execution_target_type": "account",
            "region": "us-east-1",
            "session": session,
            "dry_run": dry_run,
            "metadata": metadata,
            "dependency_data": {},
            "actions": actions,
        },
        identitystore,
        actions,
    )


def test_status_false_alone_selects_all_disabled_users() -> None:
    arguments, identitystore, actions = _arguments(metadata={"status": False})

    result = remove_idc_user.run(**arguments)

    assert result["status"] == "DISABLED"
    assert result["targeted_count"] == 2
    assert result["planned_count"] == 2
    assert identitystore.deleted == []
    assert len(actions.actions) == 2
    assert all(action.startswith("(dry-run)") for action in actions.actions)


def test_users_and_status_are_combined_and_support_username_or_email() -> None:
    arguments, identitystore, _ = _arguments(
        metadata={"users": ["alice", "bob@example.com"], "status": "disabled"},
        dry_run=False,
    )

    result = remove_idc_user.run(**arguments)

    assert result["targeted_count"] == 1
    assert result["removed_count"] == 1
    assert result["unmatched_users"] == []
    assert identitystore.deleted == ["user-2"]


def test_users_alone_can_select_multiple_native_identifiers() -> None:
    arguments, identitystore, _ = _arguments(
        metadata={"users": ["USER-1", "carol@example.com"]}, dry_run=False
    )

    result = remove_idc_user.run(**arguments)

    assert result["targeted_count"] == 2
    assert set(identitystore.deleted) == {"user-1", "user-3"}


def test_remove_idc_user_requires_at_least_one_filter() -> None:
    arguments, _, _ = _arguments(metadata={})

    with pytest.raises(RuntimeError, match="metadata.users, metadata.status"):
        remove_idc_user.run(**arguments)


@pytest.mark.parametrize("status", ["pending", 0, [], {}])
def test_remove_idc_user_rejects_invalid_status(status: object) -> None:
    arguments, _, _ = _arguments(metadata={"status": status})

    with pytest.raises(RuntimeError, match="metadata.status"):
        remove_idc_user.run(**arguments)
