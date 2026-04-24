from pathlib import Path

import pytest


def test_graph_fails_on_unknown_dependency(monkeypatch):
    try:
        from anvil.cli import main
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    yaml_dir = Path("tests") / "_tmp"
    yaml_dir.mkdir(exist_ok=True)
    yaml_file = yaml_dir / "graph-orgs.yaml"

    yaml_file.write_text(
        """
schema_version: 1
organizations:
  - name: test
    profile: test
    regions:
      - us-east-1
    tasks:
      - name: remove_iam_user
        depends_on:
          - discover
""",
        encoding="utf-8",
    )

    try:
        monkeypatch.setattr(
            "sys.argv", ["anvil", "graph", "--config-file", str(yaml_file)]
        )

        with pytest.raises(SystemExit) as exc:
            main()

        assert exc.value.code == 1
    finally:
        yaml_file.unlink(missing_ok=True)
