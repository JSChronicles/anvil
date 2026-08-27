from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from anvil.actions import ActionRecorder
from anvil.providers.aws.tasks import compare_asg_to_cluster_instances
from anvil.providers.aws.tasks import get_aws_inline_policies
from anvil.providers.aws.tasks import get_aws_sso_inline_policies
from anvil.providers.aws.tasks import get_organization_structure
from anvil.providers.aws.tasks import remove_iam_user
from anvil.providers.aws.tasks import remove_idc_user
from anvil.providers.aws.tasks import remove_missing_group_assignments
from anvil.task_errors import TaskExecutionError


def _client_error(code: str, operation_name: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": f"{code} from test"}}, operation_name
    )


class FakePaginator:
    def __init__(self, pages: Sequence[dict[str, object]]) -> None:
        self.pages = list(pages)
        self.calls: list[dict[str, object]] = []

    def paginate(self, **kwargs: object):
        self.calls.append(kwargs)
        yield from self.pages


class FakeSession:
    def __init__(self, clients: dict[str, object]) -> None:
        self.clients = clients

    def client(self, service_name: str, **_kwargs: object) -> object:
        return self.clients[service_name]


class FakeResourceNotFoundClientError(ClientError):
    pass


class FakeIdentityStoreClient:
    exceptions = SimpleNamespace(
        ResourceNotFoundException=FakeResourceNotFoundClientError
    )

    def __init__(self, error: ClientError | None = None) -> None:
        self.error = error

    def describe_group(self, **_kwargs: object) -> dict[str, object]:
        if self.error is not None:
            raise self.error
        return {}


def test_group_validation_marks_only_resource_not_found_as_missing() -> None:
    missing_error = FakeResourceNotFoundClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}},
        "DescribeGroup",
    )

    result = remove_missing_group_assignments._validate_groups(
        FakeIdentityStoreClient(missing_error), "store-1", {"group-1"}
    )

    assert result == {"group-1": False}


def test_group_validation_surfaces_unexpected_client_errors() -> None:
    error = _client_error("ThrottlingException", "DescribeGroup")

    with pytest.raises(ClientError) as raised:
        remove_missing_group_assignments._validate_groups(
            FakeIdentityStoreClient(error), "store-1", {"group-1"}
        )

    assert raised.value is error


class FakeSsoAdminClient:
    def __init__(
        self,
        *,
        delete_error: ClientError | None = None,
        deletion_status: str = "SUCCEEDED",
    ) -> None:
        self.delete_error = delete_error
        self.deletion_status = deletion_status
        self.delete_calls: list[dict[str, object]] = []

    def list_instances(self) -> dict[str, object]:
        return {
            "Instances": [
                {
                    "Status": "ACTIVE",
                    "InstanceArn": "arn:aws:sso:::instance/ssoins-1",
                    "IdentityStoreId": "store-1",
                    "OwnerAccountId": "111111111111",
                }
            ]
        }

    def delete_account_assignment(self, **kwargs: object) -> dict[str, object]:
        self.delete_calls.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error
        return {
            "AccountAssignmentDeletionStatus": {
                "Status": self.deletion_status,
                "RequestId": "request-1",
                "FailureReason": "test failure",
            }
        }


def _run_remove_missing_group_assignments(
    monkeypatch: pytest.MonkeyPatch,
    *,
    dry_run: bool,
    delete_error: ClientError | None = None,
    deletion_status: str = "SUCCEEDED",
) -> tuple[dict[str, object], list[str], FakeSsoAdminClient]:
    assignment = {
        "PermissionSetArn": "arn:aws:sso:::permissionSet/ssoins-1/ps-1",
        "PermissionSetName": "ReadOnly",
        "AccountId": "222222222222",
        "AccountName": "Workload",
        "GroupId": "group-1",
    }
    monkeypatch.setattr(
        remove_missing_group_assignments, "_get_account_cache", lambda _client: {}
    )
    monkeypatch.setattr(
        remove_missing_group_assignments,
        "_get_permission_set_name_cache",
        lambda _client, _instance_arn: {},
    )
    monkeypatch.setattr(
        remove_missing_group_assignments,
        "_collect_group_assignments",
        lambda *_args: [assignment],
    )
    monkeypatch.setattr(
        remove_missing_group_assignments,
        "_validate_groups",
        lambda *_args: {"group-1": False},
    )

    sso_admin_client = FakeSsoAdminClient(
        delete_error=delete_error, deletion_status=deletion_status
    )
    session = FakeSession(
        {
            "sso-admin": sso_admin_client,
            "identitystore": object(),
            "organizations": object(),
        }
    )
    actions = ActionRecorder(actions=[])
    result = remove_missing_group_assignments.run(
        provider="aws",
        execution_target_id="111111111111",
        execution_target_name="management",
        execution_target_type="account",
        region="us-east-1",
        session=session,
        dry_run=dry_run,
        metadata={},
        dependency_data={},
        actions=actions,
    )
    return result, actions.actions, sso_admin_client


def test_remove_missing_group_assignments_labels_dry_run_and_does_not_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, actions, client = _run_remove_missing_group_assignments(
        monkeypatch, dry_run=True
    )

    assert client.delete_calls == []
    assert result["missing_count"] == 1
    assert result["removed_count"] == 0
    assert actions == ["(dry-run) Would remove 1 missing group assignment(s)"]


def test_remove_missing_group_assignments_surfaces_delete_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = _client_error("AccessDeniedException", "DeleteAccountAssignment")

    with pytest.raises(ClientError) as raised:
        _run_remove_missing_group_assignments(
            monkeypatch, dry_run=False, delete_error=error
        )

    assert raised.value is error


def test_remove_missing_group_assignments_waits_for_terminal_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, actions, client = _run_remove_missing_group_assignments(
        monkeypatch, dry_run=False
    )

    assert result["removed_count"] == 1
    assert result["failed_count"] == 0
    assert len(client.delete_calls) == 1
    assert actions == ["Removed 1 missing group assignment(s)"]


def test_remove_missing_group_assignments_preserves_failed_status_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(TaskExecutionError) as raised:
        _run_remove_missing_group_assignments(
            monkeypatch, dry_run=False, deletion_status="FAILED"
        )

    assert raised.value.partial_result["failed_count"] == 1
    assert raised.value.partial_result["removed_count"] == 0


class FakeIamClient:
    def __init__(self) -> None:
        self.paginators = {
            "list_groups_for_user": FakePaginator(
                [{"Groups": [{"GroupName": "group-a"}]}, {"Groups": []}]
            ),
            "list_access_keys": FakePaginator(
                [
                    {"AccessKeyMetadata": [{"AccessKeyId": "key-a"}]},
                    {"AccessKeyMetadata": [{"AccessKeyId": "key-b"}]},
                ]
            ),
            "list_mfa_devices": FakePaginator([{"MFADevices": []}]),
            "list_ssh_public_keys": FakePaginator([{"SSHPublicKeys": []}]),
            "list_signing_certificates": FakePaginator([{"Certificates": []}]),
            "list_attached_user_policies": FakePaginator([{"AttachedPolicies": []}]),
            "list_user_policies": FakePaginator([{"PolicyNames": []}]),
            "list_user_tags": FakePaginator([{"Tags": []}]),
        }
        self.mutation_calls: list[tuple[str, dict[str, object]]] = []

    def get_paginator(self, operation_name: str) -> FakePaginator:
        return self.paginators[operation_name]

    def get_user(self, **_kwargs: object) -> dict[str, object]:
        return {"User": {"UserName": "alice"}}

    def list_service_specific_credentials(self, **_kwargs: object) -> dict[str, object]:
        return {"ServiceSpecificCredentials": []}

    def get_login_profile(self, **_kwargs: object) -> None:
        raise _client_error("NoSuchEntity", "GetLoginProfile")

    def remove_user_from_group(self, **kwargs: object) -> None:
        self.mutation_calls.append(("remove_user_from_group", kwargs))

    def delete_access_key(self, **kwargs: object) -> None:
        self.mutation_calls.append(("delete_access_key", kwargs))

    def delete_user(self, **kwargs: object) -> None:
        self.mutation_calls.append(("delete_user", kwargs))


def _run_remove_iam_user(
    *, dry_run: bool, metadata: dict[str, object] | None = None
) -> tuple[dict[str, object], list[str], FakeIamClient]:
    iam_client = FakeIamClient()
    actions = ActionRecorder(actions=[])
    result = remove_iam_user.run(
        provider="aws",
        execution_target_id="111111111111",
        execution_target_name="workload",
        execution_target_type="account",
        region="us-east-1",
        session=FakeSession({"iam": iam_client}),
        dry_run=dry_run,
        metadata=metadata if metadata is not None else {"users": ["alice"]},
        dependency_data={},
        actions=actions,
    )
    return result, actions.actions, iam_client


def test_remove_iam_user_cleans_resources_from_every_page() -> None:
    result, actions, client = _run_remove_iam_user(dry_run=False)

    assert client.mutation_calls == [
        ("remove_user_from_group", {"GroupName": "group-a", "UserName": "alice"}),
        ("delete_access_key", {"UserName": "alice", "AccessKeyId": "key-a"}),
        ("delete_access_key", {"UserName": "alice", "AccessKeyId": "key-b"}),
        ("delete_user", {"UserName": "alice"}),
    ]
    assert all(
        paginator.calls == [{"UserName": "alice"}]
        for paginator in client.paginators.values()
    )
    assert result["removed_count"] == 1
    assert actions == ["Removed 1 IAM user(s)"]


def test_remove_iam_user_labels_dry_run_and_does_not_mutate() -> None:
    result, actions, client = _run_remove_iam_user(
        dry_run=True, metadata={"users": ["alice"]}
    )

    assert client.mutation_calls == []
    assert result["planned_count"] == 1
    assert actions == ["(dry-run) Would remove 1 IAM user(s)"]


def test_remove_iam_user_rejects_legacy_user_name_selector() -> None:
    with pytest.raises(RuntimeError, match=r"metadata\.users"):
        _run_remove_iam_user(dry_run=True, metadata={"user_name": "alice"})


class FakeAutoScalingClient:
    def describe_auto_scaling_groups(self, **_kwargs: object) -> dict[str, object]:
        return {
            "AutoScalingGroups": [
                {"Instances": [{"InstanceId": "i-a"}, {"InstanceId": "i-b"}]}
            ]
        }


class FakeEcsClient:
    def __init__(self) -> None:
        self.paginator = FakePaginator(
            [
                {"containerInstanceArns": ["arn:container/a"]},
                {"containerInstanceArns": ["arn:container/b"]},
            ]
        )
        self.describe_calls: list[dict[str, object]] = []

    def get_paginator(self, operation_name: str) -> FakePaginator:
        assert operation_name == "list_container_instances"
        return self.paginator

    def describe_container_instances(self, **kwargs: object) -> dict[str, object]:
        self.describe_calls.append(kwargs)
        container_arn = kwargs["containerInstances"][0]
        suffix = str(container_arn).rsplit("/", maxsplit=1)[-1]
        return {
            "containerInstances": [
                {"ec2InstanceId": f"i-{suffix}", "runningTasksCount": 1}
            ]
        }


def test_compare_asg_to_cluster_instances_reads_every_ecs_page() -> None:
    ecs_client = FakeEcsClient()
    actions = ActionRecorder(actions=[])

    result = compare_asg_to_cluster_instances.run(
        provider="aws",
        execution_target_id="111111111111",
        execution_target_name="workload",
        execution_target_type="account",
        region="us-east-1",
        session=FakeSession(
            {"autoscaling": FakeAutoScalingClient(), "ecs": ecs_client}
        ),
        dry_run=False,
        metadata={"clusters": ["api"]},
        dependency_data={},
        actions=actions,
    )

    assert ecs_client.paginator.calls == [{"cluster": "api"}]
    assert ecs_client.describe_calls == [
        {"cluster": "api", "containerInstances": ["arn:container/a"]},
        {"cluster": "api", "containerInstances": ["arn:container/b"]},
    ]
    assert actions.actions == ["Completed ASG vs ECS comparison for 1 clusters"]
    assert result["missing_from_asg_count"] == 0
    assert result["zero_task_instance_count"] == 0


class FakeZeroTaskEcsClient(FakeEcsClient):
    def describe_container_instances(self, **kwargs: object) -> dict[str, object]:
        self.describe_calls.append(kwargs)
        return {
            "containerInstances": [
                {"ec2InstanceId": "i-outside-asg", "runningTasksCount": 0}
            ]
        }


def test_compare_asg_reports_missing_and_zero_task_instances_together() -> None:
    ecs_client = FakeZeroTaskEcsClient()
    result = compare_asg_to_cluster_instances.run(
        provider="aws",
        execution_target_id="111111111111",
        execution_target_name="workload",
        execution_target_type="account",
        region="us-east-1",
        session=FakeSession(
            {"autoscaling": FakeAutoScalingClient(), "ecs": ecs_client}
        ),
        dry_run=False,
        metadata={"clusters": ["api"]},
        dependency_data={},
        actions=ActionRecorder(actions=[]),
    )

    assert result["missing_from_asg_count"] == 1
    assert result["zero_task_instance_count"] == 1
    assert result["clusters"][0]["instances_missing_from_asg"] == ["i-outside-asg"]
    assert result["clusters"][0]["instances_with_zero_tasks"] == ["i-outside-asg"]


class FakeIamPolicyClient:
    def __init__(self, *, policy_error: ClientError | None = None) -> None:
        self.policy_error = policy_error

    def get_paginator(self, operation_name: str) -> FakePaginator:
        pages = {
            "list_users": [{"Users": [{"UserName": "alice"}]}],
            "list_user_policies": [{"PolicyNames": ["inline-a"]}],
        }
        return FakePaginator(pages[operation_name])

    def get_user_policy(self, **_kwargs: object) -> dict[str, object]:
        if self.policy_error is not None:
            raise self.policy_error
        return {"PolicyDocument": {"Version": "2012-10-17", "Statement": []}}


def _run_inline_policy_inventory(client: FakeIamPolicyClient) -> dict[str, object]:
    return get_aws_inline_policies.run(
        provider="aws",
        execution_target_id="111111111111",
        execution_target_name="workload",
        execution_target_type="account",
        region="us-east-1",
        session=FakeSession({"iam": client}),
        dry_run=False,
        metadata={"types": ["user"]},
        dependency_data={},
        actions=ActionRecorder(actions=[]),
    )


def test_inline_policy_inventory_returns_collected_policies() -> None:
    result = _run_inline_policy_inventory(FakeIamPolicyClient())

    assert result["policy_count"] == 1
    assert result["error_count"] == 0


def test_inline_policy_inventory_preserves_partial_failures() -> None:
    with pytest.raises(TaskExecutionError) as raised:
        _run_inline_policy_inventory(
            FakeIamPolicyClient(
                policy_error=_client_error("AccessDenied", "GetUserPolicy")
            )
        )

    assert raised.value.partial_result["policy_count"] == 0
    assert raised.value.partial_result["error_count"] == 1


@pytest.mark.parametrize(
    ("task_module", "expected_scope"),
    [
        (remove_iam_user, "target"),
        (get_aws_inline_policies, "target"),
        (remove_idc_user, "configured_target"),
        (remove_missing_group_assignments, "configured_target"),
        (get_organization_structure, "configured_target"),
        (get_aws_sso_inline_policies, "configured_target"),
    ],
)
def test_account_wide_aws_tasks_run_once_per_intended_boundary(
    task_module, expected_scope: str
) -> None:
    assert task_module.TASK_SCOPE == expected_scope


class FakeOrganizationClient:
    def get_paginator(self, operation_name: str):
        assert operation_name == "list_organizational_units_for_parent"
        return self

    def paginate(self, **kwargs: object):
        parent_id = kwargs["ParentId"]
        assert isinstance(parent_id, str)
        children = {
            "r-root": [{"Id": "ou-parent", "Name": "Parent"}],
            "ou-parent": [{"Id": "ou-child", "Name": "Child"}],
            "ou-child": [],
        }
        yield {"OrganizationalUnits": children[parent_id]}


def test_organization_discovery_recurses_and_preserves_parents() -> None:
    result = get_organization_structure._list_all_organizational_units(
        "r-root", FakeOrganizationClient()
    )

    assert result == [
        {"Id": "ou-parent", "Name": "Parent", "ParentId": "r-root"},
        {"Id": "ou-child", "Name": "Child", "ParentId": "ou-parent"},
    ]
