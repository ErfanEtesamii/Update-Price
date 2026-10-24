"""Exception hierarchy shared across the app.

Kept flat and explicit so the update service and Telegram reporting layer
can distinguish outcome categories (see spec section 13) without inspecting
string messages.
"""
from __future__ import annotations


class FarazHonarError(Exception):
    """Base class for all application-raised errors."""


class ConfigurationError(FarazHonarError):
    """Required configuration/credentials/documentation is missing.

    Raised at the point a feature is actually used, never at import time,
    so the rest of the app keeps working around the missing piece.
    """


class ProductDataError(FarazHonarError):
    """A product's row in the source sheet is invalid or incomplete."""


class MissingProductIdentifierError(ProductDataError):
    """A product has no usable platform identifier (e.g. no DigiKala link)."""


class PlatformAPIError(FarazHonarError):
    """Base class for errors returned by a platform's API."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class PlatformAuthError(PlatformAPIError):
    """Authentication/token failure talking to a platform API."""


class VariantResolutionError(PlatformAPIError):
    """Could not determine the platform-specific product identifier."""


class DigikalaReferencePriceRejected(PlatformAPIError):
    """DigiKala rejected selling_price with its own reference-price-range
    validation ("قیمت فروش باید در بازه قیمت مرجع باشد") — this is DigiKala's
    live business rule, not our own decision, so it is classified as
    OutcomeStatus.CAPPED_12PCT (not FAILED_API) by update.service. Always
    non-retryable: the same target price will be rejected again immediately,
    and only changes once DigiKala's own reference-price bracket changes
    (e.g. via an approved ticket).
    """


class RetryExhaustedError(FarazHonarError):
    """All retry attempts for an operation were used up."""

    def __init__(self, message: str, *, last_error: Exception | None = None):
        super().__init__(message)
        self.last_error = last_error


class CrossPlatformStateError(FarazHonarError):
    """A result/progress update for one platform was about to be recorded
    into another platform's run state (RunSummary/JobProgress).

    This must never happen — every platform's execution/result/report state
    is strictly isolated (see core.models.RunSummary.add_result and
    telegram_bot.state.JobProgress.record) — so this is always a bug
    upstream, never a normal/expected condition, and is deliberately never
    caught and silenced anywhere in this codebase.
    """
