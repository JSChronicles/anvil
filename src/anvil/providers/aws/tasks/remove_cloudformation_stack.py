from __future__ import annotations

import logging

from botocore.exceptions import ClientError, WaiterError

from anvil.actions import ActionRecorder
from anvil.providers.tasks._task_helpers import metadata_bool
from anvil.task_errors import TaskExecutionError

__LOGGER__ = logging.getLogger(__name__)
TASK_SCOPE = "target"

# Bounded wait for DELETE_COMPLETE. 30 * 20s = 10 minutes.
_DELETE_WAITER_CONFIG = {"Delay": 20, "MaxAttempts": 30}


def _force_retain_stuck_resources(metadata: dict[str, object]) -> bool:
    """Return whether to retry a failed delete by retaining stuck resources."""

    return metadata_bool(
        task_name="remove_cloudformation_stack",
        metadata=metadata,
        key="force_retain_stuck_resources",
        default=False,
    )


def _delete_failed_logical_ids(cfn_client, stack_id: str) -> list[str]:
    """Return the logical IDs of resources currently stuck in DELETE_FAILED."""

    resources = cfn_client.describe_stack_resources(StackName=stack_id)[
        "StackResources"
    ]
    return [
        resource["LogicalResourceId"]
        for resource in resources
        if resource.get("ResourceStatus") == "DELETE_FAILED"
    ]


def _selected_stack_ids(
    metadata: dict[str, object], *, execution_target_id: str
) -> list[str]:
    """Return this account's exact stack identifiers from a shared account map.

    ``metadata.stack_ids_by_account`` is one mapping shared by every account a
    target resolves; each invocation looks up only its own
    ``execution_target_id`` entry, so one target/one metadata block can carry
    a different, explicit stack selection per account without matching or
    discovery. An account present in the target's ``include`` list but absent
    from the map is treated as "nothing to do" for that account.
    """

    stack_map = metadata.get("stack_ids_by_account")
    if not isinstance(stack_map, dict) or not stack_map:
        raise RuntimeError(
            "remove_cloudformation_stack requires metadata.stack_ids_by_account "
            "to be a non-empty mapping of account ID to an array of stack IDs"
        )

    account_stack_ids = stack_map.get(execution_target_id, [])
    if not isinstance(account_stack_ids, list) or not all(
        isinstance(item, str) and item.strip() for item in account_stack_ids
    ):
        raise RuntimeError(
            "remove_cloudformation_stack requires "
            f"metadata.stack_ids_by_account['{execution_target_id}'] to be an "
            "array of non-empty strings"
        )
    return [item.strip() for item in account_stack_ids]


def _describe_stack(cfn_client, stack_id: str) -> dict[str, object] | None:
    """Return one stack's description, or None if it no longer exists.

    ``describe_stacks`` keeps returning a deleted stack's record (with
    ``StackStatus: DELETE_COMPLETE``) for a period after deletion when
    queried by exact ARN -- it only raises "does not exist" once that
    record ages out. A stack already in ``DELETE_COMPLETE`` is treated the
    same as one that raised "does not exist": there's nothing left to do.
    """

    try:
        stacks = cfn_client.describe_stacks(StackName=stack_id)["Stacks"]
    except ClientError as error:
        if "does not exist" in error.response["Error"].get("Message", ""):
            return None
        raise
    stack = stacks[0] if stacks else None
    if stack is not None and stack.get("StackStatus") == "DELETE_COMPLETE":
        return None
    return stack


def run(
    *,
    provider: str,
    execution_target_id: str,
    execution_target_name: str,
    execution_target_type: str,
    region: str,
    session,
    dry_run: bool,
    metadata: dict[str, object],
    dependency_data: dict[str, object],
    actions: ActionRecorder,
) -> dict[str, object]:
    """Delete one or more explicitly named CloudFormation stacks.

    This target-scoped AWS task deletes the exact stacks assigned to the
    current account in ``metadata.stack_ids_by_account`` -- full stack ARNs
    are recommended over bare names, since an ARN can never resolve to the
    wrong stack even if a name is reused later. The same mapping is shared
    across every account a target resolves; each invocation only reads its
    own ``execution_target_id`` entry, so one target can carry a distinct,
    explicit stack selection per account without any name matching or
    discovery. It is meant for decommissioning specific, already identified
    stacks -- for example, an orphaned or superseded StackSet-managed stack
    instance whose resources block a replacement deployment. In dry-run mode
    it reports the current status of each stack without deleting anything.
    When not in dry-run mode, it waits (bounded) for each deletion to reach
    ``DELETE_COMPLETE``.

    Metadata:
        stack_ids_by_account: Required non-empty mapping of AWS account ID to
            an array of exact stack names or ARNs to remove in that account.
            An account missing from the mapping is a no-op.
        force_retain_stuck_resources: Optional boolean, defaults to false.
            When the initial delete fails, retries once with
            ``RetainResources`` set to whatever logical IDs are currently
            reported as ``DELETE_FAILED`` -- for example, a custom resource
            this account doesn't own or control. This makes the *stack*
            delete unconditionally, but any retained resource is left
            behind, still existing, no longer managed by any stack. Use this
            only when leaving that specific resource behind is acceptable.

    Args:
        provider: Provider name for the current execution target.
        execution_target_id: Target AWS account ID.
        execution_target_name: Friendly name for the target account.
        execution_target_type: Provider target type.
        region: Current AWS region.
        session: Boto3 session scoped to the current region.
        dry_run: Whether execution is running in dry-run mode.
        metadata: Task metadata containing the exact stack selectors.
        dependency_data: Runtime data selected from declared task dependencies.
        actions: Action recorder provided by the engine.

    Returns:
        A payload with ``removed_stacks``, ``skipped_stacks``, and
        ``failed_stacks`` (empty in dry-run mode, since nothing is removed).
        Each removed stack includes ``retained_resources``: the logical IDs
        (if any) left behind because of ``force_retain_stuck_resources``.

    Raises:
        RuntimeError: If ``metadata.stack_ids_by_account`` is missing or invalid.
        botocore.exceptions.ClientError: If an unexpected AWS API error occurs.
        TaskExecutionError: If one or more selected stacks fail to be removed.
    """
    stack_ids = _selected_stack_ids(metadata, execution_target_id=execution_target_id)
    force_retain = _force_retain_stuck_resources(metadata)
    cfn_client = session.client("cloudformation")

    planned_count = 0
    removed: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []

    for stack_id in stack_ids:
        try:
            stack = _describe_stack(cfn_client, stack_id)
            if stack is None:
                skipped.append({"stack_id": stack_id, "reason": "not_found"})
                __LOGGER__.info(f"Stack '{stack_id}' does not exist; skipping")
                continue

            stack_name = stack["StackName"]
            stack_status = stack["StackStatus"]

            if dry_run:
                planned_count += 1
                __LOGGER__.info(
                    f"(dry-run) Would delete stack '{stack_name}' "
                    f"(currently {stack_status})"
                )
                continue

            stack_result: dict[str, object] = {
                "stack_id": stack_id,
                "stack_name": stack_name,
                "stack_status": stack_status,
            }

            cfn_client.delete_stack(StackName=stack_id)
            __LOGGER__.info(f"Delete initiated for stack '{stack_name}'")
            retained_resources: list[str] = []
            try:
                cfn_client.get_waiter("stack_delete_complete").wait(
                    StackName=stack_id, WaiterConfig=_DELETE_WAITER_CONFIG
                )
            except WaiterError as error:
                if not force_retain:
                    raise RuntimeError(
                        f"Timed out or failed waiting for '{stack_name}' to "
                        f"delete: {error}"
                    ) from error

                retained_resources = _delete_failed_logical_ids(cfn_client, stack_id)
                if not retained_resources:
                    raise RuntimeError(
                        f"'{stack_name}' failed to delete and no DELETE_FAILED "
                        f"resources were found to retain: {error}"
                    ) from error

                __LOGGER__.warning(
                    f"'{stack_name}' failed to delete; retrying while retaining "
                    f"stuck resource(s): {retained_resources}"
                )
                cfn_client.delete_stack(
                    StackName=stack_id, RetainResources=retained_resources
                )
                try:
                    cfn_client.get_waiter("stack_delete_complete").wait(
                        StackName=stack_id, WaiterConfig=_DELETE_WAITER_CONFIG
                    )
                except WaiterError as retry_error:
                    raise RuntimeError(
                        f"'{stack_name}' still failed to delete after retaining "
                        f"{retained_resources}: {retry_error}"
                    ) from retry_error

            stack_result["retained_resources"] = retained_resources
            removed.append(stack_result)
            if retained_resources:
                __LOGGER__.warning(
                    f"Deleted stack '{stack_name}', retaining: {retained_resources}"
                )
            else:
                __LOGGER__.info(f"Deleted stack '{stack_name}'")
        except (ClientError, RuntimeError) as error:
            failed.append({"stack_id": stack_id, "error": str(error)})
            __LOGGER__.warning(f"Failed to remove stack '{stack_id}': {error}")

    result: dict[str, object] = {
        "removed_stacks": removed,
        "skipped_stacks": skipped,
        "failed_stacks": failed,
    }
    if dry_run:
        actions.record(f"(dry-run) Would remove {planned_count} stack(s)")
    else:
        actions.record(f"Removed {len(removed)} stack(s)")

    if failed:
        raise TaskExecutionError(
            f"remove_cloudformation_stack failed to remove {len(failed)} of "
            f"{len(stack_ids)} selected stack(s)",
            partial_result=result,
        )
    return result
