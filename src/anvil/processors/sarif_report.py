from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from anvil.results import TargetResult

if TYPE_CHECKING:
    from anvil.processor_loader import ProcessorRunContext


DEFAULT_OUTPUT = "anvil.sarif"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_VERSION = "2.1.0"
VALID_LEVELS = {"none", "note", "warning", "error"}


def run(
    *, context: ProcessorRunContext, output: str | None, metadata: dict[str, object]
) -> dict[str, object]:
    """Write a SARIF 2.1.0 report from task `sarif_findings` records.

    The processor scans task results for explicit `sarif_findings` payloads and
    converts them into a SARIF report suitable for code scanning and security
    tooling.

    Metadata:
        tool_name: Optional SARIF tool driver name. Defaults to `Anvil`.
        automation_category: Optional SARIF automation details ID.

    Args:
        context: Completed run context provided by the Anvil processor runner.
        output: Optional output path. Defaults to `reports/anvil.sarif` under
            the run directory.
        metadata: Processor metadata containing optional SARIF settings.

    Returns:
        A payload containing the written output path, finding count, and rule
        count.

    Raises:
        RuntimeError: If any `sarif_findings` record is malformed.
    """
    output_path = Path(output) if output is not None else _default_output_path(context)
    sarif_results, rules = _collect_sarif_results(context=context)
    payload = _build_sarif(metadata=metadata, rules=rules, results=sarif_results)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    return {
        "output": str(output_path),
        "finding_count": len(sarif_results),
        "rule_count": len(rules),
    }


def _default_output_path(context: ProcessorRunContext) -> Path:
    return context.run_dir / "reports" / DEFAULT_OUTPUT


def _collect_sarif_results(
    *, context: ProcessorRunContext
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    sarif_results: list[dict[str, object]] = []
    rules: dict[str, dict[str, object]] = {}

    for target_result in _target_result_dicts(context=context):
        configured_entity: dict[str, object] = {"type": "configured_target"}
        for task_result in _target_task_results(target_result=target_result):
            _collect_task_findings(
                sarif_results=sarif_results,
                rules=rules,
                target_result=target_result,
                entity_result=configured_entity,
                task_result=task_result,
            )
        for entity_result in _entity_results(target_result=target_result):
            for task_result in _task_results(entity_result=entity_result):
                _collect_task_findings(
                    sarif_results=sarif_results,
                    rules=rules,
                    target_result=target_result,
                    entity_result=entity_result,
                    task_result=task_result,
                )

    return sarif_results, rules


def _target_result_dicts(*, context: ProcessorRunContext) -> list[dict[str, object]]:
    if context.target_result is not None:
        return [_target_result_dict(context.target_result)]

    return [
        _target_result_dict(target_result) for target_result in context.target_results
    ]


def _target_result_dict(
    target_result: TargetResult | dict[str, object],
) -> dict[str, object]:
    if isinstance(target_result, TargetResult):
        return target_result.to_dict()

    return target_result


def _entity_results(*, target_result: dict[str, object]) -> list[dict[str, object]]:
    entities = target_result.get("entities", [])
    if not isinstance(entities, list):
        return []

    return [
        cast(dict[str, object], item) for item in entities if isinstance(item, dict)
    ]


def _task_results(*, entity_result: dict[str, object]) -> list[dict[str, object]]:
    task_results = entity_result.get("tasks", [])
    if not isinstance(task_results, list):
        return []

    return [
        cast(dict[str, object], item) for item in task_results if isinstance(item, dict)
    ]


def _target_task_results(
    *, target_result: dict[str, object]
) -> list[dict[str, object]]:
    return _task_results(entity_result=target_result)


def _collect_task_findings(
    *,
    sarif_results: list[dict[str, object]],
    rules: dict[str, dict[str, object]],
    target_result: dict[str, object],
    entity_result: dict[str, object],
    task_result: dict[str, object],
) -> None:
    result = task_result.get("result")
    if not isinstance(result, dict) or "sarif_findings" not in result:
        return

    raw_findings = result.get("sarif_findings")
    if not isinstance(raw_findings, list):
        raise RuntimeError("sarif_report requires result.sarif_findings to be a list")

    for raw_finding in raw_findings:
        if not isinstance(raw_finding, dict):
            raise RuntimeError(
                "sarif_report requires every sarif_findings entry to be a mapping"
            )
        raw_finding = cast(dict[str, object], raw_finding)
        sarif_result, rule = _convert_finding(
            finding=raw_finding,
            target_result=target_result,
            entity_result=entity_result,
            task_result=task_result,
        )
        rule_id = _required_string(rule, "id", "finding.rule")
        existing_rule = rules.get(rule_id)
        if existing_rule is not None and existing_rule != rule:
            raise RuntimeError(
                f"sarif_report found conflicting metadata for rule {rule_id!r}"
            )
        rules[rule_id] = rule
        sarif_results.append(sarif_result)


def _convert_finding(
    *,
    finding: dict[str, object],
    target_result: dict[str, object],
    entity_result: dict[str, object],
    task_result: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    rule = _rule_descriptor(finding=finding)
    rule_id = _required_string(rule, "id", "finding.rule")
    message = _required_string(finding, "message", "finding")
    locations = _locations(finding=finding)
    level = _level(finding.get("level") or rule.get("level"))
    fingerprint = finding.get("fingerprint")

    result: dict[str, object] = {
        "ruleId": rule_id,
        "level": level,
        "message": {"text": message},
        "locations": locations,
        "properties": _result_properties(
            finding=finding,
            target_result=target_result,
            entity_result=entity_result,
            task_result=task_result,
        ),
    }
    if isinstance(fingerprint, str) and fingerprint.strip():
        result["partialFingerprints"] = {"anvilResourceFinding": fingerprint.strip()}

    return result, rule


def _rule_descriptor(*, finding: dict[str, object]) -> dict[str, object]:
    raw_rule = finding.get("rule")
    if not isinstance(raw_rule, dict):
        raise RuntimeError("sarif_report requires finding.rule to be a mapping")
    raw_rule = cast(dict[str, object], raw_rule)

    rule_id = _required_string(raw_rule, "id", "finding.rule")
    rule: dict[str, object] = {"id": rule_id}

    for source_key, target_key in (
        ("name", "name"),
        ("short_description", "shortDescription"),
        ("full_description", "fullDescription"),
    ):
        value = raw_rule.get(source_key)
        if isinstance(value, str) and value.strip():
            if target_key == "name":
                rule[target_key] = value.strip()
            else:
                rule[target_key] = {"text": value.strip()}

    help_markdown = raw_rule.get("help_markdown")
    if isinstance(help_markdown, str) and help_markdown.strip():
        rule["help"] = {"markdown": help_markdown.strip()}

    level = _level(raw_rule.get("level", "warning"))
    rule["defaultConfiguration"] = {"level": level}
    rule["properties"] = _rule_properties(raw_rule)
    rule["level"] = level

    return rule


def _rule_properties(raw_rule: dict[str, object]) -> dict[str, object]:
    properties: dict[str, object] = {}
    tags = raw_rule.get("tags")
    if isinstance(tags, list):
        properties["tags"] = [tag for tag in tags if isinstance(tag, str)]

    for source_key, target_key in (
        ("security_severity", "security-severity"),
        ("precision", "precision"),
    ):
        value = raw_rule.get(source_key)
        if isinstance(value, str) and value.strip():
            properties[target_key] = value.strip()

    return properties


def _locations(*, finding: dict[str, object]) -> list[dict[str, object]]:
    raw_locations = finding.get("locations")
    if not isinstance(raw_locations, list) or len(raw_locations) == 0:
        raise RuntimeError(
            "sarif_report requires finding.locations to be a non-empty list"
        )

    return [_location(raw_location) for raw_location in raw_locations]


def _location(raw_location: object) -> dict[str, object]:
    if not isinstance(raw_location, dict):
        raise RuntimeError("sarif_report requires every location to be a mapping")
    raw_location = cast(dict[str, object], raw_location)

    uri = _required_string(raw_location, "uri", "finding.location")
    physical_location: dict[str, object] = {"artifactLocation": {"uri": uri}}

    region = _region(raw_location)
    if region:
        physical_location["region"] = region

    location: dict[str, object] = {"physicalLocation": physical_location}
    message = raw_location.get("message")
    if isinstance(message, str) and message.strip():
        location["message"] = {"text": message.strip()}

    properties = raw_location.get("properties")
    if isinstance(properties, dict) and properties:
        location["properties"] = properties

    return location


def _region(raw_location: dict[str, object]) -> dict[str, object]:
    region: dict[str, object] = {}
    for key in ("startLine", "startColumn", "endLine", "endColumn"):
        value = raw_location.get(key)
        if isinstance(value, int):
            region[key] = value

    return region


def _result_properties(
    *,
    finding: dict[str, object],
    target_result: dict[str, object],
    entity_result: dict[str, object],
    task_result: dict[str, object],
) -> dict[str, object]:
    target_key = "target"

    raw_properties = finding.get("properties")
    properties: dict[str, object] = (
        dict(cast(dict[str, object], raw_properties))
        if isinstance(raw_properties, dict)
        else {}
    )
    properties.update(
        {
            "target_type": target_key,
            "target": target_result.get(target_key) or target_result.get("target"),
            "entity_id": entity_result.get("id"),
            "entity_name": entity_result.get("name"),
            "entity_type": entity_result.get("type"),
            "region": task_result.get("region"),
            "task_id": task_result.get("task_id"),
            "task_name": task_result.get("task_name"),
        }
    )

    return {key: value for key, value in properties.items() if value is not None}


def _build_sarif(
    *,
    metadata: dict[str, object],
    rules: dict[str, dict[str, object]],
    results: list[dict[str, object]],
) -> dict[str, object]:
    tool_name = _metadata_string(metadata=metadata, key="tool_name", default="Anvil")
    run: dict[str, object] = {
        "tool": {
            "driver": {
                "name": tool_name,
                "rules": [_strip_internal_rule_fields(rule) for rule in rules.values()],
            }
        },
        "results": results,
    }

    automation_category = metadata.get("automation_category")
    if isinstance(automation_category, str) and automation_category.strip():
        run["automationDetails"] = {"id": automation_category.strip()}

    return {"version": SARIF_VERSION, "$schema": SARIF_SCHEMA, "runs": [run]}


def _strip_internal_rule_fields(rule: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in rule.items() if key != "level"}


def _required_string(mapping: dict[str, object], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"sarif_report requires {label}.{key} to be a string")

    return value.strip()


def _metadata_string(*, metadata: dict[str, object], key: str, default: str) -> str:
    value = metadata.get(key, default)
    if isinstance(value, str) and value.strip():
        return value.strip()

    return default


def _level(value: object) -> str:
    if isinstance(value, str) and value in VALID_LEVELS:
        return value

    raise RuntimeError(
        "sarif_report requires finding levels to be one of: "
        f"{', '.join(sorted(VALID_LEVELS))}"
    )
