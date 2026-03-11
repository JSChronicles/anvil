import pytest

from anvil.cli import main


def test_cli_no_args_exits(monkeypatch):
    monkeypatch.setattr("sys.argv", ["anvil"])
    with pytest.raises(SystemExit):
        main()
