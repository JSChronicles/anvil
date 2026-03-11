import pytest

from anvil.cli import main


def test_graph_fails_on_unknown_dependency(monkeypatch, tmp_path):
    yaml_file = tmp_path / "orgs.yaml"

    yaml_file.write_text(
        """
organizations:
  - name: test
    profile: test
    region: us-east-1
    tasks:
      - name: remove_iam_user
        depends_on:
          - discover
"""
    )

    monkeypatch.setattr("sys.argv", ["anvil", "graph", "--org-file", str(yaml_file)])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
