from __future__ import annotations

from collections.abc import Sequence

from anvil.providers.aws.tasks import inactive_account_cloudtrail_signal
from anvil.providers.aws.tasks import inactive_account_context
from anvil.providers.aws.tasks import inactive_account_cost_signal
from anvil.providers.aws.tasks import inactive_account_report
from anvil.providers.aws.tasks import inactive_account_resource_signal


class FakePaginator:
    def __init__(self, pages: Sequence[dict[str, object]]) -> None:
        self.pages = list(pages)
        self.calls: list[dict[str, object]] = []

    def paginate(self, **kwargs: object):
        self.calls.append(kwargs)
        yield from self.pages


class FakeOrganizationsClient:
    def __init__(self) -> None:
        self.accounts = FakePaginator(
            [
                {
                    "Accounts": [
                        {
                            "Id": "111111111111",
                            "Name": "payments",
                            "Email": "payments@example.com",
                            "State": "ACTIVE",
                        },
                        {"Id": "222222222222", "Name": "closed", "State": "SUSPENDED"},
                    ]
                }
            ]
        )
        self.tags = FakePaginator(
            [
                {
                    "Tags": [
                        {"Key": "Owner", "Value": "platform-team"},
                        {"Key": "Environment", "Value": "production"},
                    ]
                }
            ]
        )

    def get_paginator(self, operation_name: str) -> FakePaginator:
        return {"list_accounts": self.accounts, "list_tags_for_resource": self.tags}[
            operation_name
        ]


def test_context_returns_account_tags_as_non_scoring_context() -> None:
    result = inactive_account_context.collect_accounts(
        FakeOrganizationsClient(), ("Owner", "TechnicalOwner")
    )

    assert result == [
        {
            "account_id": "111111111111",
            "account_name": "payments",
            "email": "payments@example.com",
            "state": "ACTIVE",
            "tags": {"Owner": "platform-team", "Environment": "production"},
            "ownership_tags": {"Owner": "platform-team"},
            "warnings": [],
        }
    ]


class FakeCostExplorerClient:
    def get_cost_and_usage(self, **_kwargs: object) -> dict[str, object]:
        return {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-06-01"},
                    "Groups": [
                        {
                            "Keys": ["111111111111"],
                            "Metrics": {"UnblendedCost": {"Amount": "12.00"}},
                        }
                    ],
                },
                {
                    "TimePeriod": {"Start": "2026-07-01"},
                    "Groups": [
                        {
                            "Keys": ["111111111111"],
                            "Metrics": {"UnblendedCost": {"Amount": "6.00"}},
                        }
                    ],
                },
                {
                    "TimePeriod": {"Start": "2026-08-01"},
                    "Groups": [
                        {
                            "Keys": ["111111111111"],
                            "Metrics": {"UnblendedCost": {"Amount": "0.00"}},
                        }
                    ],
                },
            ]
        }


def test_cost_signal_groups_months_by_linked_account() -> None:
    result = inactive_account_cost_signal.collect_cost_signals(
        FakeCostExplorerClient(), [{"account_id": "111111111111"}]
    )["111111111111"]

    assert result["total_cost_3m"] == 18.0
    assert result["avg_monthly_cost_3m"] == 6.0
    assert result["cost_trend"] == "decreasing"
    assert result["cost_score"] == 100
    assert result["warnings"] == []


class FakeTaggingClient:
    def get_paginator(self, operation_name: str) -> FakePaginator:
        assert operation_name == "get_resources"
        return FakePaginator(
            [
                {
                    "ResourceTagMappingList": [
                        {
                            "ResourceARN": "arn:aws:lambda:us-east-1:111111111111:function:checkout",
                            "Tags": [
                                {"Key": "Owner", "Value": "payments-team"},
                                {"Key": "Environment", "Value": "production"},
                            ],
                        },
                        {
                            "ResourceARN": "arn:aws:rds:us-east-1:111111111111:db:ledger",
                            "Tags": [{"Key": "Environment", "Value": "production"}],
                        },
                    ]
                }
            ]
        )


def test_resource_ownership_preserves_resource_level_contacts() -> None:
    result = inactive_account_resource_signal.collect_resource_ownership(
        FakeTaggingClient(), ("Owner", "TechnicalOwner")
    )

    assert result["owner_counts"] == {"payments-team": 1}
    assert result["recognized_owner_resource_count"] == 1
    assert result["owned_resources"] == [
        {
            "resource_arn": "arn:aws:lambda:us-east-1:111111111111:function:checkout",
            "resource_type": "lambda:function",
            "ownership_tags": {"Owner": "payments-team"},
        }
    ]


def test_report_fans_in_regions_and_keeps_tags_out_of_score() -> None:
    account = {
        "account_id": "111111111111",
        "account_name": "payments",
        "tags": {"Owner": "account-owner"},
        "ownership_tags": {"Owner": "account-owner"},
    }
    cost = {"111111111111": {"cost_score": 100, "warnings": []}}
    activity = {
        "111111111111": {"activity_score": 100, "iam_usage_score": 100, "warnings": []}
    }
    regional_resources = [
        {
            "account_id": "111111111111",
            "region": "us-east-1",
            "resource_count_complete": True,
            "resource_counts": {"lambda_functions": 1},
            "resource_ownership": {
                "owner_counts": {"payments-team": 1},
                "recognized_owner_resource_count": 1,
                "resources_without_recognized_owner_count": 0,
                "owned_resources": [
                    {
                        "resource_arn": "arn:aws:lambda:us-east-1:111111111111:function:checkout"
                    }
                ],
            },
            "warnings": [],
        },
        {
            "account_id": "111111111111",
            "region": "us-west-2",
            "resource_count_complete": True,
            "resource_counts": {"lambda_functions": 2},
            "resource_ownership": {
                "owner_counts": {"payments-team": 2},
                "recognized_owner_resource_count": 2,
                "resources_without_recognized_owner_count": 0,
                "owned_resources": [],
            },
            "warnings": [],
        },
    ]

    result = inactive_account_report.build_report(
        accounts=[account],
        costs=cost,
        activity=activity,
        regional_resources=regional_resources,
    )

    assessment = result["accounts"][0]
    assert assessment["signals"]["resources"]["resource_count"] == 3
    assert assessment["scores"] == {
        "cost_score": 100,
        "activity_score": 100,
        "iam_usage_score": 100,
        "resource_score": 70,
    }
    assert assessment["final_score"] == 96
    assert assessment["ownership_context"]["resource_ownership"]["owner_counts"] == {
        "payments-team": 3
    }
    assert "payments-team" in assessment["recommendation"]


def test_inactive_account_task_scopes_match_resource_ownership_boundaries() -> None:
    assert inactive_account_context.TASK_SCOPE == "configured_target"
    assert inactive_account_cost_signal.TASK_SCOPE == "configured_target"
    assert inactive_account_cloudtrail_signal.TASK_SCOPE == "configured_target"
    assert inactive_account_report.TASK_SCOPE == "configured_target"
    assert not hasattr(inactive_account_resource_signal, "TASK_SCOPE")
