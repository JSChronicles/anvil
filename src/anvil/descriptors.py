from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class OrgDescriptor:
    """
    Declarative description of a single AWS Organization execution context.
    """

    name: str
    profile: str | None = None
    region: str = "us-east-1"
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
