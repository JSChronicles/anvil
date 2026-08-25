"""Shared GitHub organization task helpers."""

from anvil.providers.github.tasks._rest import require_github_provider


def organization_for_task(
    *, task_name: str, provider: str, execution_target_type: str, session
):
    """Return the current GitHub organization object."""

    require_github_provider(task_name=task_name, provider=provider)
    if execution_target_type != "organization":
        raise RuntimeError(f"{task_name} requires a GitHub organization target")
    operation = getattr(session.client, "get_organization", None)
    if not callable(operation):
        raise RuntimeError(f"{task_name} requires GitHub get_organization()")
    return operation(session.target_id)


def github_identity(item: object) -> dict[str, object]:
    """Return stable public identity fields from a PyGithub object."""

    fields = ("id", "login", "slug", "name", "email", "description", "privacy")
    return {
        field: value
        for field in fields
        if (value := getattr(item, field, None)) is not None
    }
