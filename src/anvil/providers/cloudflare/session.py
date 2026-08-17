"""Cloudflare SDK client construction, discovery, and runtime sessions."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import ModuleType

from anvil.providers.cloudflare.auth import CloudflareAuthSettings

CLOUDFLARE_EXTRA_REMEDIATION = (
    "Install Cloudflare dependencies with 'uv sync --extra cloudflare' for a "
    "source checkout or 'pip install \"anvil[cloudflare]\"' for an installed "
    "package."
)


class CloudflareDependencyError(RuntimeError):
    """Raised when the optional Cloudflare SDK cannot be imported."""


@dataclass(frozen=True, slots=True)
class CloudflareAccount:
    """Cloudflare account identity returned by account discovery."""

    account_id: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class CloudflareZone:
    """Cloudflare zone identity returned by zone discovery."""

    zone_id: str
    display_name: str | None = None
    account_id: str | None = None
    account_name: str | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class CloudflareSession:
    """Cloudflare runtime session for one account or zone target."""

    client: object
    target_type: str
    target_id: str
    region_name: str
    auth_source: str
    account_id: str | None = None
    zone_id: str | None = None


class CloudflareSessionFactory:
    """Build Cloudflare clients lazily and own their resource lifecycle."""

    def validate_client(self, *, settings: CloudflareAuthSettings) -> None:
        """Validate that credentials can construct an SDK client.

        This intentionally performs no network request because Cloudflare user
        tokens, account-owned tokens, and legacy keys have different useful
        verification boundaries.
        """

        with self._client(settings=settings):
            return

    def list_accounts(
        self, *, settings: CloudflareAuthSettings
    ) -> list[CloudflareAccount]:
        """List every account visible to the configured credential."""

        try:
            with self._client(settings=settings) as client:
                accounts_resource = getattr(client, "accounts", None)
                list_accounts = getattr(accounts_resource, "list", None)
                if not callable(list_accounts):
                    raise RuntimeError(
                        "Cloudflare SDK client does not expose accounts.list()"
                    )
                accounts = [
                    self._account_from_sdk(item) for item in list_accounts(per_page=50)
                ]
        except Exception as error:
            raise self._discovery_error(
                resource="accounts", error=error, settings=settings
            ) from None
        return sorted(accounts, key=lambda account: account.account_id)

    def list_zones(
        self, *, settings: CloudflareAuthSettings, account_id: str | None = None
    ) -> list[CloudflareZone]:
        """List every visible zone, optionally bounded to one account."""

        try:
            with self._client(settings=settings) as client:
                zones_resource = getattr(client, "zones", None)
                list_zones = getattr(zones_resource, "list", None)
                if not callable(list_zones):
                    raise RuntimeError(
                        "Cloudflare SDK client does not expose zones.list()"
                    )
                parameters: dict[str, object] = {"per_page": 50}
                if account_id is not None:
                    parameters["account"] = {"id": account_id}
                zones = [
                    self._zone_from_sdk(item=item, configured_account_id=account_id)
                    for item in list_zones(**parameters)
                ]
        except Exception as error:
            scope = f" for account '{account_id}'" if account_id is not None else ""
            raise self._discovery_error(
                resource=f"zones{scope}", error=error, settings=settings
            ) from None
        return sorted(zones, key=lambda zone: zone.zone_id)

    def create_session(
        self,
        *,
        settings: CloudflareAuthSettings,
        target_type: str,
        target_id: str,
        region_name: str,
        account_id: str | None,
        zone_id: str | None,
    ) -> CloudflareSession:
        """Create a Cloudflare runtime session for one execution target."""

        try:
            client = self._build_client(settings=settings)
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError(
                f"Cloudflare provider could not build a runtime session for "
                f"{target_type} '{target_id}': "
                f"{self._safe_error_detail(error=error, settings=settings)}"
            ) from None
        return CloudflareSession(
            client=client,
            target_type=target_type,
            target_id=target_id,
            region_name=region_name,
            auth_source=settings.source,
            account_id=account_id,
            zone_id=zone_id,
        )

    def close_session(self, *, session: CloudflareSession) -> None:
        """Close a runtime session's underlying SDK client."""

        self._close_client(session.client)

    @contextmanager
    def _client(self, *, settings: CloudflareAuthSettings) -> Iterator[object]:
        client = self._build_client(settings=settings)
        try:
            yield client
        except BaseException as operation_error:
            try:
                self._close_client(client)
            except RuntimeError as cleanup_error:
                operation_error.add_note(str(cleanup_error))
            raise
        else:
            self._close_client(client)

    def _build_client(self, *, settings: CloudflareAuthSettings) -> object:
        cloudflare_module = self._load_cloudflare()
        client_class = getattr(cloudflare_module, "Cloudflare", None)
        if not callable(client_class):
            raise RuntimeError("Cloudflare SDK does not expose Cloudflare")

        parameters: dict[str, object] = {}
        if settings.api_token is not None:
            parameters["api_token"] = settings.api_token
        else:
            parameters["api_key"] = settings.api_key
            parameters["api_email"] = settings.api_email
        if settings.base_url is not None:
            parameters["base_url"] = settings.base_url

        try:
            return client_class(**parameters)
        except Exception as error:
            raise RuntimeError(
                "Cloudflare provider could not construct the SDK client: "
                f"{self._safe_error_detail(error=error, settings=settings)}"
            ) from None

    @classmethod
    def _discovery_error(
        cls, *, resource: str, error: Exception, settings: CloudflareAuthSettings
    ) -> RuntimeError:
        """Map SDK discovery failures to actionable, secret-safe errors."""

        status_code = cls._status_code(error)
        if status_code == 401:
            return RuntimeError(
                f"Cloudflare authentication failed while discovering {resource} "
                "(HTTP 401). Check that the API token or legacy global API key and "
                "email are valid."
            )
        if status_code == 403:
            if resource == "accounts":
                return RuntimeError(
                    "Cloudflare credential is not authorized to discover accounts "
                    "(HTTP 403). Use credentials authorized for Cloudflare account "
                    "listing, or configure explicit account IDs with include."
                )
            return RuntimeError(
                f"Cloudflare credential is not authorized to discover {resource} "
                "(HTTP 403). Grant Zone Read access for the intended zones, or "
                "configure explicit zone IDs with include."
            )
        if status_code == 429:
            return RuntimeError(
                f"Cloudflare rate limited discovery of {resource} (HTTP 429) after "
                "SDK retries. Retry later or reduce concurrent API use."
            )
        detail = cls._safe_error_detail(error=error, settings=settings)
        status = f" (HTTP {status_code})" if status_code is not None else ""
        return RuntimeError(
            f"Cloudflare provider could not discover {resource}{status}: {detail}"
        )

    @staticmethod
    def _status_code(error: Exception) -> int | None:
        """Return an HTTP status exposed directly or through an SDK response."""

        status_code = getattr(error, "status_code", None)
        if isinstance(status_code, int):
            return status_code
        response = getattr(error, "response", None)
        response_status = getattr(response, "status_code", None)
        return response_status if isinstance(response_status, int) else None

    @staticmethod
    def _safe_error_detail(
        *, error: Exception, settings: CloudflareAuthSettings
    ) -> str:
        """Return external error text with resolved credential values redacted."""

        detail = str(error) or type(error).__name__
        for secret in (settings.api_token, settings.api_key, settings.api_email):
            if secret:
                detail = detail.replace(secret, "[redacted]")
        return detail

    @staticmethod
    def _close_client(client: object) -> None:
        """Close a client without exposing arbitrary SDK error details."""

        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception as error:
                raise RuntimeError(
                    f"Cloudflare SDK client cleanup failed ({type(error).__name__})."
                ) from None

    @staticmethod
    def _load_cloudflare() -> ModuleType:
        try:
            import cloudflare
        except ImportError as error:
            raise CloudflareDependencyError(
                "Cloudflare provider requires optional dependency 'cloudflare'. "
                f"{CLOUDFLARE_EXTRA_REMEDIATION}"
            ) from error
        return cloudflare

    @classmethod
    def _account_from_sdk(cls, item: object) -> CloudflareAccount:
        account_id = cls._required_sdk_string(item, "id", response_type="account")
        display_name = cls._optional_sdk_string(item, "name")
        return CloudflareAccount(account_id=account_id, display_name=display_name)

    @classmethod
    def _zone_from_sdk(
        cls, *, item: object, configured_account_id: str | None
    ) -> CloudflareZone:
        account = cls._sdk_value(item, "account")
        account_id = cls._optional_sdk_string(account, "id")
        zone_id = cls._required_sdk_string(item, "id", response_type="zone")
        if (
            configured_account_id is not None
            and account_id is not None
            and account_id != configured_account_id
        ):
            raise RuntimeError(
                f"Cloudflare zone discovery returned zone '{zone_id}' for account "
                f"'{account_id}' outside configured account '{configured_account_id}'"
            )
        return CloudflareZone(
            zone_id=zone_id,
            display_name=cls._optional_sdk_string(item, "name"),
            account_id=account_id or configured_account_id,
            account_name=cls._optional_sdk_string(account, "name"),
            status=cls._optional_sdk_string(item, "status"),
        )

    @classmethod
    def _required_sdk_string(
        cls, item: object, field_name: str, *, response_type: str
    ) -> str:
        value = cls._optional_sdk_string(item, field_name)
        if value is None:
            raise RuntimeError(
                f"Cloudflare {response_type} discovery returned an item without "
                f"a valid {field_name}"
            )
        return value

    @classmethod
    def _optional_sdk_string(cls, item: object, field_name: str) -> str | None:
        value = cls._sdk_value(item, field_name)
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _sdk_value(item: object, field_name: str) -> object | None:
        if isinstance(item, Mapping):
            return item.get(field_name)
        return getattr(item, field_name, None)
