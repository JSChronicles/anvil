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
schema_version: 2
targets:
  - name: test
    provider:
      name: aws
      mode: organization
      options:
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


@pytest.mark.parametrize(
    ("provider", "mode", "task_name", "include"),
    [
        (
            "azure",
            "subscriptions",
            "count_resource_groups",
            "00000000-0000-0000-0000-000000000000",
        ),
        ("gcp", "projects", "get_project_info", "anvil-dev-project"),
    ],
)
def test_graph_resolves_provider_specific_tasks(
    monkeypatch, tmp_path, capsys, provider, mode, task_name, include
):
    try:
        from anvil.cli import main
    except PermissionError as error:
        pytest.skip(f"jsonschema package resources unavailable in test env: {error}")

    yaml_file = tmp_path / f"graph-{provider}.yaml"
    yaml_file.write_text(
        f"""
schema_version: 2
targets:
  - name: {provider}-target
    provider:
      name: {provider}
      mode: {mode}
      options: {{}}
    regions:
      - {"eastus" if provider == "azure" else "global"}
    include:
      - {include}
    tasks:
      - name: {task_name}
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "sys.argv", ["anvil", "graph", "--config-file", str(yaml_file), "--json"]
    )

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert f'"target": "{provider}-target"' in output
    assert f'"name": "{task_name}"' in output
