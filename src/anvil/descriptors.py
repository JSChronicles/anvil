from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TargetDescriptor:
    """
    Declarative description of one execution target group loaded from config.

    Public configs load from schema_version: 2 top-level targets.
    """

    name: str
    provider: str
    mode: str
    regions: list[str] | None = None
    tasks: list[dict[str, object]] = field(default_factory=lambda: [{"name": "noop"}])
    post_run: list[dict[str, object]] = field(default_factory=list)

    max_workers: int = 10
    max_parallel_regions: int = 1
    fail_fast: bool = False
    dry_run: bool = False

    include: list[str] | None = None
    exclude: list[str] | None = None

    metadata: dict[str, object] = field(default_factory=dict)
    provider_options: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str):
            raise ValueError("provider must be a string")

        normalized_provider = self.provider.strip().lower()
        if not normalized_provider:
            raise ValueError("provider must be a non-empty string")
        object.__setattr__(self, "provider", normalized_provider)

        self._validate_provider_options()
        self._normalize_mode()

        if self.max_workers < 1:
            raise ValueError("max_workers must be >= 1")

        normalized_post_run = self._normalize_post_run(self.post_run)
        object.__setattr__(self, "post_run", normalized_post_run)

        if not 1 <= self.max_parallel_regions <= 4:
            raise ValueError("max_parallel_regions must be between 1 and 4")

        if self.regions == []:
            raise ValueError("regions must contain at least one region")
        if self.regions is not None:
            normalized_regions = [region.strip() for region in self.regions]
            if any(not region for region in normalized_regions):
                raise ValueError("regions must not contain empty values")
            if len(set(normalized_regions)) != len(normalized_regions):
                raise ValueError("regions must not contain duplicates")
            object.__setattr__(self, "regions", normalized_regions)

        normalized_include = self._normalize_target_ids(self.include)
        normalized_exclude = self._normalize_target_ids(self.exclude)

        object.__setattr__(self, "include", normalized_include)
        object.__setattr__(self, "exclude", normalized_exclude)

        if self.include is not None and self.exclude is not None:
            raise ValueError("include and exclude cannot both be set")

    def _validate_provider_options(self) -> None:
        if not isinstance(self.provider_options, dict):
            raise ValueError("provider.options must be a mapping")

        if any(
            not isinstance(option_name, str) or not option_name.strip()
            for option_name in self.provider_options
        ):
            raise ValueError("provider.options keys must be non-empty strings")

    def _normalize_mode(self) -> None:
        if not isinstance(self.mode, str):
            raise ValueError("mode must be a string")
        mode = self.mode.strip().lower()
        if not mode:
            raise ValueError("mode must be a non-empty string")

        object.__setattr__(self, "mode", mode)

    @staticmethod
    def _normalize_target_ids(target_ids: list[str] | None) -> list[str] | None:
        if target_ids is None:
            return None

        normalized = [target_id.strip() for target_id in target_ids]

        if any(not target_id for target_id in normalized):
            raise ValueError("target ID lists must not contain empty values")

        if len(set(normalized)) != len(normalized):
            raise ValueError("target ID lists must not contain duplicates")

        return normalized

    @staticmethod
    def _normalize_post_run(
        post_run: list[dict[str, object]] | None,
    ) -> list[dict[str, object]]:
        if post_run is None:
            return []

        normalized: list[dict[str, object]] = []
        for index, raw_spec in enumerate(post_run, start=1):
            if not isinstance(raw_spec, dict):
                raise ValueError(f"post_run entry #{index} must be a mapping")

            processor = raw_spec.get("processor")
            if not isinstance(processor, str) or not processor.strip():
                raise ValueError(
                    f"post_run entry #{index} requires a non-empty processor"
                )

            output = raw_spec.get("output")
            if output is not None and not isinstance(output, str):
                raise ValueError(f"post_run entry #{index} output must be a string")

            metadata = raw_spec.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError(f"post_run entry #{index} metadata must be a mapping")

            run_on_failure = raw_spec.get("run_on_failure", False)
            if not isinstance(run_on_failure, bool):
                raise ValueError(
                    f"post_run entry #{index} run_on_failure must be a boolean"
                )

            normalized_spec: dict[str, object] = {
                "processor": processor.strip(),
                "metadata": dict(metadata),
            }
            if output is not None:
                normalized_spec["output"] = output
            if run_on_failure:
                normalized_spec["run_on_failure"] = run_on_failure

            normalized.append(normalized_spec)

        return normalized


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    targets: list[TargetDescriptor]
    max_parallel_targets: int = 1
