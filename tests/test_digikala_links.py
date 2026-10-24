import pytest

from faraz_honar.core.exceptions import VariantResolutionError
from faraz_honar.platforms.digikala.links import extract_dkp_code


def test_bare_numeric_code_passes_through():
    assert extract_dkp_code("20589157") == "20589157"


def test_extracts_from_full_product_url():
    url = "https://www.digikala.com/product/dkp-20589157/some-slug/"
    assert extract_dkp_code(url) == "20589157"


def test_extracts_from_url_without_trailing_slug():
    assert extract_dkp_code("https://www.digikala.com/product/dkp-12345") == "12345"


def test_strips_whitespace():
    assert extract_dkp_code("  20589157  ") == "20589157"


def test_unrecognized_value_raises():
    with pytest.raises(VariantResolutionError, match="Could not extract"):
        extract_dkp_code("not-a-valid-reference")
