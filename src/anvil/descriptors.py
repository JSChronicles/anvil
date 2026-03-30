from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class OrgDescriptor:
    """
    Declarative description of a single AWS Organization execution context.
    """

    name: str
    profile: str | None = None
    regions: list[str] = field(default_factory=lambda: ["us-east-1"])
    role_name: str = "OrganizationAccountAccessRole"
    tasks: list[dict[str, object]] = field(default_factory=lambda: [{"name": "noop"}])

    max_workers: int = 10
    fail_fast: bool = False
    dry_run: bool = False

    include: list[str] | None = None
    exclude: list[str] | None = None

    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_workers < 1:
            raise ValueError("max_workers must be >= 1")

        if self.include and self.exclude:
            raise ValueError("include and exclude cannot both be set")

        if not self.regions:
            raise ValueError("regions must contain at least one region")

        normalized_regions = [region.strip() for region in self.regions]
        if any(not region for region in normalized_regions):
            raise ValueError("regions must not contain empty values")

        if len(set(normalized_regions)) != len(normalized_regions):
            raise ValueError("regions must not contain duplicates")

        object.__setattr__(self, "regions", normalized_regions)
