"""Shared PagerDuty task helpers."""

from anvil.providers.tasks._task_helpers import bounded, metadata_int


def list_resources(
    *, task_name: str, session, resource: str, metadata: dict[str, object]
) -> list[object]:
    """List a bounded PagerDuty REST collection."""

    max_results = metadata_int(
        task_name=task_name, metadata=metadata, key="max_results"
    )
    iterator = getattr(session.client, "iter_all", None)
    if not callable(iterator):
        raise RuntimeError("PagerDuty client does not expose iter_all()")
    return bounded(iterator(resource), max_results=max_results)
