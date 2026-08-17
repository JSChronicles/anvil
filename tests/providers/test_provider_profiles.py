from __future__ import annotations

from pathlib import Path

import pytest

from anvil.provider_profiles import (
    ANVIL_CONFIG_ENV,
    ANVIL_CONFIG_PATH,
    ProviderProfileConfig,
)


def test_provider_profile_config_uses_anvil_config_environment_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "profiles.toml"
    monkeypatch.setenv(ANVIL_CONFIG_ENV, str(config_path))

    assert ProviderProfileConfig().path == config_path


def test_provider_profile_config_defaults_to_anvil_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ANVIL_CONFIG_ENV, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert ProviderProfileConfig().path == tmp_path / ANVIL_CONFIG_PATH


def test_provider_profile_config_loads_only_requested_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ANVIL_CONFIG_ENV, raising=False)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[providers.github.work]\ntoken_env = " GITHUB_WORK_TOKEN "\n'
        '[providers.gitlab.work]\ntoken_env = "GITLAB_WORK_TOKEN"\n',
        encoding="utf-8",
    )

    profiles = ProviderProfileConfig(path=config_path).load(
        provider_name="github", supported_options={"token_env"}
    )

    assert profiles == {"work": {"token_env": "GITHUB_WORK_TOKEN"}}


def test_provider_profile_config_returns_empty_for_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ANVIL_CONFIG_ENV, raising=False)

    profiles = ProviderProfileConfig(path=tmp_path / "missing.toml").load(
        provider_name="github"
    )

    assert profiles == {}


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("providers = []\n", "key 'providers' must be a table"),
        ('[providers]\ngithub = "invalid"\n', "Provider 'github'.*must be a table"),
        ('[providers.github]\nwork = "invalid"\n', "profile 'work'.*must be a table"),
        (
            '[providers.github.work]\ntoken_env = "TOKEN"\nunknown = "value"\n',
            "unsupported option 'unknown'",
        ),
        ('[providers.github.work]\ntoken_env = "  "\n', "must be a non-empty string"),
        ("[providers.github.work]\napp_id = 123\n", "must be a non-empty string"),
    ],
)
def test_provider_profile_config_rejects_invalid_schema(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: str, message: str
) -> None:
    monkeypatch.delenv(ANVIL_CONFIG_ENV, raising=False)
    config_path = tmp_path / "config.toml"
    config_path.write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        ProviderProfileConfig(path=config_path).load(
            provider_name="github", supported_options={"token_env", "app_id"}
        )
