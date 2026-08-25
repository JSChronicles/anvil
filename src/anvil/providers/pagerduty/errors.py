"""PagerDuty provider error taxonomy."""


class PagerDutyProviderError(RuntimeError):
    """Base class for actionable PagerDuty provider failures."""


class PagerDutyCredentialError(PagerDutyProviderError):
    """Raised when the configured PagerDuty credential is missing."""


class PagerDutyDependencyError(PagerDutyProviderError):
    """Raised when the optional PagerDuty SDK cannot be used."""


class PagerDutyClientError(PagerDutyProviderError):
    """Raised when the PagerDuty REST client cannot be initialized or closed."""
