import pytest

import requests

from faraz_honar.core.exceptions import (
    ConfigurationError,
    DigikalaReferencePriceRejected,
    MissingProductIdentifierError,
    PlatformAPIError,
    PlatformAuthError,
    ProductDataError,
    RetryExhaustedError,
    VariantResolutionError,
)
from faraz_honar.core.retry import call_with_retry, is_transient_error


def test_succeeds_on_first_try():
    calls = []

    def op():
        calls.append(1)
        return "ok"

    assert call_with_retry(op, max_attempts=3, backoff_seconds=0) == "ok"
    assert len(calls) == 1


def test_retries_retryable_error_then_succeeds():
    attempts = {"n": 0}

    def op():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PlatformAPIError("temporary", retryable=True)
        return "ok"

    assert call_with_retry(op, max_attempts=3, backoff_seconds=0) == "ok"
    assert attempts["n"] == 3


def test_gives_up_after_max_attempts():
    attempts = {"n": 0}

    def op():
        attempts["n"] += 1
        raise PlatformAPIError("still failing", retryable=True)

    with pytest.raises(RetryExhaustedError):
        call_with_retry(op, max_attempts=3, backoff_seconds=0)
    assert attempts["n"] == 3


def test_non_retryable_error_stops_immediately():
    attempts = {"n": 0}

    def op():
        attempts["n"] += 1
        raise PlatformAPIError("bad data", retryable=False)

    with pytest.raises(RetryExhaustedError):
        call_with_retry(op, max_attempts=3, backoff_seconds=0)
    assert attempts["n"] == 1


# --- is_transient_error ------------------------------------------------------


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_transient_status_codes_are_transient(status):
    assert is_transient_error(PlatformAPIError("x", status_code=status, retryable=False))


@pytest.mark.parametrize("status", [400, 404, 422])
def test_permanent_status_codes_are_not_transient(status):
    assert not is_transient_error(PlatformAPIError("x", status_code=status, retryable=False))


def test_permanent_status_wins_even_if_flagged_retryable():
    assert not is_transient_error(PlatformAPIError("x", status_code=404, retryable=True))


def test_other_4xx_without_retryable_flag_is_not_transient():
    assert not is_transient_error(PlatformAPIError("x", status_code=403, retryable=False))


def test_retryable_flag_without_status_is_transient():
    # How the clients wrap requests.RequestException (timeout / connection reset).
    assert is_transient_error(PlatformAPIError("request failed", retryable=True))


def test_api_error_without_status_and_not_retryable_is_not_transient():
    assert not is_transient_error(PlatformAPIError("unrecognized shape", retryable=False))


def test_variant_resolution_error_with_429_is_transient_by_status_not_class():
    # Digikala's "variant search failed: HTTP 429" must be retried.
    assert is_transient_error(VariantResolutionError("search failed", status_code=429))


def test_variant_resolution_error_without_status_is_permanent():
    # "No variant matches this DKP code" / missing reference in the sheet.
    assert not is_transient_error(VariantResolutionError("not found", retryable=False))


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("t"),
        ConnectionError("c"),
        requests.exceptions.Timeout("t"),
        requests.exceptions.ConnectionError("c"),
    ],
)
def test_raw_network_errors_are_transient(exc):
    assert is_transient_error(exc)


@pytest.mark.parametrize(
    "exc",
    [
        PlatformAuthError("auth", status_code=401),
        # Never transient even if a client mislabels the status/retryable flag.
        PlatformAuthError("auth", status_code=503, retryable=True),
        ProductDataError("bad row"),
        MissingProductIdentifierError("no id"),
        ConfigurationError("missing"),
        DigikalaReferencePriceRejected("range", status_code=400),
        DigikalaReferencePriceRejected("range", status_code=429, retryable=True),
    ],
)
def test_never_transient_exception_types(exc):
    assert not is_transient_error(exc)


@pytest.mark.parametrize("exc", [None, ValueError("boom"), KeyError("k"), RuntimeError("r")])
def test_unrelated_or_missing_errors_are_not_transient(exc):
    assert not is_transient_error(exc)
