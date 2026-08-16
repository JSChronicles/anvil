from __future__ import annotations

import os
import tomllib
from collections.abc import Collection
from pathlib import Path

ANVIL_CONFIG_ENV = "ANVIL_CONFIG"
ANVIL_CONFIG_PATH = Path(".anvil") / "config.toml"


class ProviderProfileConfig:
    """Load provider profiles from Anvil's user configuration file."""

    def __init__(self, *, path: Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        """Return the configured Anvil profile file path."""

        configured_path = os.environ.get(ANVIL_CONFIG_ENV)
        if configured_path:
            return Path(configured_path).expanduser()
        if self._path is not None:
            return self._path.expanduser()
        return Path.home() / ANVIL_CONFIG_PATH

    def load(
        self, *, provider_name: str, supported_options: Collection[str] | None = None
    ) -> dict[str, dict[str, str]]:
        """Return profiles for one provider, keyed by profile name.

        Args:
            provider_name: Provider namespace below the ``providers`` table.
            supported_options: Optional provider-owned allowlist for profile options.

        Raises:
            RuntimeError: If the file cannot be read or its schema is invalid.
        """

        path = self.path
        if not path.exists():
            return {}

        try:
            config = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            raise RuntimeError(f"Anvil config '{path}' is invalid: {error}") from error
        except OSError as error:
            raise RuntimeError(
                f"Anvil config '{path}' could not be read: {error}"
            ) from error

        raw_providers = config.get("providers", {})
        if not isinstance(raw_providers, dict):
            raise RuntimeError(f"Anvil config '{path}' key 'providers' must be a table")

        raw_profiles = raw_providers.get(provider_name, {})
        if not isinstance(raw_profiles, dict):
            raise RuntimeError(
                f"Provider '{provider_name}' in Anvil config '{path}' must be a table"
            )

        profiles: dict[str, dict[str, str]] = {}
        for profile_name, raw_profile in raw_profiles.items():
            if not isinstance(raw_profile, dict):
                raise RuntimeError(
                    f"Provider '{provider_name}' profile '{profile_name}' in Anvil "
                    f"config '{path}' must be a table"
                )

            profile: dict[str, str] = {}
            for option_name, option_value in raw_profile.items():
                if (
                    supported_options is not None
                    and option_name not in supported_options
                ):
                    raise RuntimeError(
                        f"Provider '{provider_name}' profile '{profile_name}' in Anvil "
                        f"config '{path}' has unsupported option '{option_name}'"
                    )
                if not isinstance(option_value, str) or not option_value.strip():
                    raise RuntimeError(
                        f"Provider '{provider_name}' profile '{profile_name}' option "
                        f"'{option_name}' must be a non-empty string"
                    )
                profile[option_name] = option_value.strip()
            profiles[profile_name] = profile

        return profiles
