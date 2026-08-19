from __future__ import annotations

import os
import threading
import tomllib
from collections.abc import Collection, Mapping
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


class ProviderProfileResolver:
    """Merge provider profile settings with target-specific provider options."""

    def __init__(
        self,
        *,
        provider_name: str,
        profile_options: Collection[str],
        config: ProviderProfileConfig | None = None,
    ) -> None:
        self._provider_name = provider_name
        self._profile_options = frozenset(profile_options)
        self._config = config or ProviderProfileConfig()
        self._cache: tuple[Path, dict[str, dict[str, str]]] | None = None
        self._lock = threading.Lock()

    def resolve(self, provider_options: Mapping[str, object]) -> dict[str, object]:
        """Resolve a named/default profile without mixing inline profile fields.

        Options outside ``profile_options`` are target-specific and remain inline.
        This allows, for example, a Cloudflare ``account_id`` to accompany a named
        authentication profile.

        Args:
            provider_options: Options configured on one provider target.

        Returns:
            Provider options with profile settings expanded and ``profile`` removed.

        Raises:
            RuntimeError: If a requested profile is missing or invalid.
            ValueError: If ``profile`` is invalid or mixed with inline profile fields.
        """

        options = dict(provider_options)
        raw_profile_name = options.pop("profile", None)
        profile_name: str | None = None
        if raw_profile_name is not None:
            if not isinstance(raw_profile_name, str) or not raw_profile_name.strip():
                raise ValueError("provider.options.profile must be a non-empty string")
            profile_name = raw_profile_name.strip()

        inline_profile_options = sorted(self._profile_options.intersection(options))
        if profile_name is not None:
            if inline_profile_options:
                joined = ", ".join(inline_profile_options)
                raise ValueError(
                    f"{self._provider_name} provider.options.profile cannot be "
                    f"combined with inline profile options: {joined}"
                )
            profiles = self._load_profiles()
            profile = profiles.get(profile_name)
            if profile is None:
                raise RuntimeError(
                    f"{self._provider_name} profile '{profile_name}' was not found "
                    f"in '{self._config.path}'"
                )
            return {**profile, **options}

        if inline_profile_options:
            return options

        default_profile = self._load_profiles().get("default")
        if default_profile is None:
            return options
        return {**default_profile, **options}

    def _load_profiles(self) -> dict[str, dict[str, str]]:
        """Load and cache profiles for the resolver's provider namespace."""

        path = self._config.path
        with self._lock:
            if self._cache is not None and self._cache[0] == path:
                return {name: dict(profile) for name, profile in self._cache[1].items()}
            profiles = self._config.load(
                provider_name=self._provider_name,
                supported_options=self._profile_options,
            )
            cached_profiles = {
                name: dict(profile) for name, profile in profiles.items()
            }
            self._cache = (path, cached_profiles)
            return {name: dict(profile) for name, profile in cached_profiles.items()}
