# Data Modeling And Naming

## Prefer @dataclass For Structured Objects

When structured objects are needed, prefer `@dataclass` over manual boilerplate classes. Use `frozen=True` when immutability is appropriate.

Avoid:

```python
class AccountResult:
    def __init__(self, account_id: str, region: str, success: bool) -> None:
        self.account_id = account_id
        self.region = region
        self.success = success
```

Prefer:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class AccountResult:
    """Result of processing an account in a region."""

    account_id: str
    region: str
    success: bool
```

Rules:

- Prefer dataclasses for stable data containers.
- Use dictionaries for dynamic external data, JSON-like payloads, and schema-less mappings.
- Use `frozen=True` when the value should not change after construction.
- Consider `slots=True` for large numbers of instances when it improves memory behavior without hurting clarity.

## Descriptive Variable Names

Variable names must clearly describe the value they contain. Single-character names are not allowed except for mathematical expressions.

Avoid:

```python
x = proj_data["project"]["name"]
d = data["items"]
i = 0
```

Prefer:

```python
project_name = proj_data["project"]["name"]
items = data["items"]
index = 0
```

## Avoid Dictionary Aliasing And Pass-Through Variables

Do not introduce variables that simply mirror a dictionary access when the original structure is already short and readable.

Avoid:

```python
project = proj_data["project"]
name = project["name"]
```

Prefer:

```python
project_name = proj_data["project"]["name"]
state = proj_data["project"]["person"]["state"]
```

Aliases are acceptable when:

- The access path is deeply nested or complex.
- The value represents an expensive computation.
- The value must capture moment-in-time state.
- The variable introduces meaningful semantic meaning not already present.