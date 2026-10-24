"""Normalizes whatever the sheet's DigiKala column holds into a DKP code.

This only relies on DigiKala's public, documented URL convention
(`https://www.digikala.com/product/dkp-<digits>/...`), not any private
seller-API contract — so it's safe to implement without the API docs. Your
sample sheet's DigiKala column already holds bare numeric codes (e.g.
"20589157"), which are passed through unchanged; a full product link is
also accepted in case some rows store the link instead.

The DKP code is NOT the seller-api VARIANT_ID — DigiKala product pages and
seller-api variants are different identifier spaces. This is only the
normalization step before the (still missing) documented lookup call that
turns a DKP code into a VARIANT_ID.
"""
from __future__ import annotations

import re

from faraz_honar.core.exceptions import VariantResolutionError

_DKP_URL_PATTERN = re.compile(r"dkp-(\d+)", re.IGNORECASE)
_BARE_DIGITS_PATTERN = re.compile(r"^\d+$")


def extract_dkp_code(raw_ref: str) -> str:
    raw_ref = raw_ref.strip()

    match = _DKP_URL_PATTERN.search(raw_ref)
    if match:
        return match.group(1)

    if _BARE_DIGITS_PATTERN.match(raw_ref):
        return raw_ref

    raise VariantResolutionError(
        f"Could not extract a DigiKala DKP code from sheet value '{raw_ref}': it is "
        "neither a digikala.com product URL (.../dkp-<digits>/...) nor a bare numeric code.",
        retryable=False,
    )
