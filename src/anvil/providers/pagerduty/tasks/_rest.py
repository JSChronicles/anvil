"""Shared PagerDuty task helpers."""

from anvil.providers.tasks._task_helpers import bounded, metadata_int


def list_resources(
    *, session, resource: str, metadata: dict[str, object]
) -> list[object]:
    """List a bounded PagerDuty REST collection."""

    max_results = metadata_int(metadata=metadata, key="max_results")
    iterator = getattr(session.client, "iter_all", None)
    if not callable(iterator):
        raise RuntimeError("PagerDuty client does not expose iter_all()")
    return bounded(iterator(resource), max_results=max_results)
