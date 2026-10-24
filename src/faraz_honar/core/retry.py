"""Generic retry helper (spec section 12: attempt 1, 2, 3, then stop).

Only exceptions explicitly marked retryable are retried. Auth failures,
invalid-data errors, and missing-identifier errors are never retried here —
those are handled once by the caller (e.g. a single token refresh) rather
than by blind repetition.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

import requests

from faraz_honar.core.exceptions import (
    ConfigurationError,
    DigikalaReferencePriceRejected,
    PlatformAPIError,
    PlatformAuthError,
    ProductDataError,
    RetryExhaustedError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, PlatformAPIError):
        return exc.retryable
    # Network-level errors (timeouts, connection resets) are transient by nature.
    return isinstance(exc, (TimeoutError, ConnectionError))


# HTTP statuses that mean "try again later" rather than "this request is wrong".
_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429})

# Client errors that are permanent for a given product: re-sending the same
# request can never succeed, so they must never enter an error round even if
# some client mistakenly flagged them retryable.
_PERMANENT_STATUS_CODES = frozenset({400, 404, 422})

# Exception types that are never transient, regardless of status/retryable.
# ProductDataError also covers MissingProductIdentifierError.
_NEVER_TRANSIENT = (
    PlatformAuthError,
    ProductDataError,
    ConfigurationError,
    DigikalaReferencePriceRejected,
)

# Raw network-level failures. `requests.Timeout`/`requests.ConnectionError`
# are not subclasses of the builtin TimeoutError/ConnectionError, so they are
# listed explicitly (the platform clients normally wrap them into
# PlatformAPIError(retryable=True), which is handled above them).
_NETWORK_ERRORS = (
    TimeoutError,
    ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)


def is_transient_error(exc: BaseException | None) -> bool:
    """True if `exc` is an *accidental* failure worth retrying in a later
    error round (rate limit, server error, timeout, dropped connection).

    Decided on the exception itself, never on OutcomeStatus: FAILED_API mixes
    transient (429/5xx) with permanent (400/404) errors. For PlatformAPIError
    subclasses the decision is based on `status_code`/`retryable`, not the
    class name — e.g. a VariantResolutionError carrying status_code=429 is
    transient, while one without a status ("no variant matches this DKP")
    is permanent.
    """
    if exc is None:
        return False
    if isinstance(exc, _NEVER_TRANSIENT):
        return False
    if isinstance(exc, PlatformAPIError):
        code = exc.status_code
        if code is not None:
            if code in _PERMANENT_STATUS_CODES:
                return False
            if code in _TRANSIENT_STATUS_CODES or code >= 500:
                return True
        return bool(exc.retryable)
    return isinstance(exc, _NETWORK_ERRORS)


def call_with_retry(
    func: Callable[[], T],
    *,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
    operation_name: str = "operation",
) -> T:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - intentionally broad, re-raised below
            last_error = exc
            retryable = is_retryable(exc)
            logger.warning(
                "%s failed on attempt %d/%d (retryable=%s): %s",
                operation_name,
                attempt,
                max_attempts,
                retryable,
                exc,
            )
            if not retryable or attempt == max_attempts:
                break
            time.sleep(backoff_seconds * attempt)

    raise RetryExhaustedError(
        f"{operation_name} failed after {max_attempts} attempt(s)", last_error=last_error
    ) from last_error
