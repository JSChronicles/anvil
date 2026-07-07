# Type Hints And Docstrings

## Type Hinting

Use native type hinting without importing types from `typing` unless necessary.

Avoid:

```python
from typing import Dict, List


def build_index(items: List[str]) -> Dict[str, int]:
    return {item: index for index, item in enumerate(items)}
```

Prefer:

```python
def build_index(items: list[str]) -> dict[str, int]:
    return {item: index for index, item in enumerate(items)}
```

Rules:

- Use `list[str]`, `dict[str, int]`, `tuple[str, ...]`, and `str | None`.
- Type public functions, methods, dataclass fields, and class attributes.
- Let obvious local variables infer naturally unless an explicit type improves clarity.

## Google-Style Docstrings

Use Google-style docstrings to document all functions and classes.

Avoid:

```python
def load_config(path: str) -> dict:
    """Load config."""
```

Prefer:

```python
def load_config(path: str) -> dict[str, object]:
    """Load configuration from a JSON file.

    Args:
        path: Path to the configuration file.

    Returns:
        The parsed configuration data.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file content is invalid.
    """
```

Google-style docstrings improve readability, maintainability, and consistency across the codebase.

For Anvil task modules, the `run()` docstring is also user-facing CLI help:
`anvil list --tasks <task_name> --detail` displays it. Make task docstrings
operator-focused by describing what the task checks or changes, required
metadata keys, returned result shape, and expected `RuntimeError` failures.
