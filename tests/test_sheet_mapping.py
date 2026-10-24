from pathlib import Path

from faraz_honar.core.pricing import compute_price_to_apply
from faraz_honar.sheets.csv_source import CSVProductSource
import pytest

from faraz_honar.sheets.mapping import SheetMapping, _parse_price, rows_to_products, toman_to_rial

FIXTURES = Path(__file__).parent / "fixtures"
MAPPING_PATH = Path(__file__).parents[1] / "config" / "sheet_mapping.yaml"

_TEST_MAPPING = SheetMapping(
    name_column="اسم",
    display_name_column="اسم جدید",
    reference_price_column="قیمت",
    target_price_column="قیمت اصلی",
)


def test_mapping_loads_from_yaml():
    mapping = SheetMapping.load(MAPPING_PATH)
    assert mapping.name_column == "اسم"
    # Main account -> "digikala", fast account -> "digikala2" (renamed 2026-09;
    # the sheet's column headers themselves did not change).
    assert mapping.platform_ref_columns["digikala"] == "دیجی کالا اصلی"
    assert mapping.platform_ref_columns["digikala2"] == "دیجی کالا سریع"
    assert mapping.target_price_column == "قیمت اصلی"


def test_mapping_has_no_leftover_old_digikala_keys():
    mapping = SheetMapping.load(MAPPING_PATH)
    assert "digikala_asli" not in mapping.platform_ref_columns
    assert "digikala_fast" not in mapping.platform_ref_columns


def test_mapping_maps_snappshop_isfahan_to_its_own_column():
    mapping = SheetMapping.load(MAPPING_PATH)
    assert mapping.platform_ref_columns["snappshop_isfahan"] == "اسنپ شاپ اصفهان"
    # Isfahan and Tehran are matched against different sheet columns.
    isfahan_col = mapping.platform_ref_columns["snappshop_isfahan"]
    tehran_col = mapping.platform_ref_columns["snappshop_tehran"]
    assert isfahan_col != tehran_col


def test_mapping_maps_tapsi_shop_to_its_own_column():
    mapping = SheetMapping.load(MAPPING_PATH)
    assert mapping.platform_ref_columns["tapsi_shop"] == "تپسی شاپ"


def test_csv_source_parses_sample_sheet():
    mapping = SheetMapping.load(MAPPING_PATH)
    source = CSVProductSource(FIXTURES / "sample_product_sheet.csv", mapping)
    products = source.fetch_products()

    assert len(products) > 50

    first = products[0]
    assert first.internal_id  # non-empty
    assert first.reference_price is not None
    assert first.reference_price > 0

    # spot-check a known row from the live sheet: 11cm diamond-cut nut bowl.
    # It has no "دیجی کالا سریع" value in the sample sheet (only a handful of
    # rows do, as of the 2026-09-19 read) -> no "digikala2" ref at all, even
    # though it DOES have a "دیجی کالا اصلی" value (-> its "digikala" ref) —
    # proving each column only ever feeds its own platform. It also has no
    # "قیمت اصلی" -> target_price stays None, which is correct: not every
    # product has a target price set yet.
    no_target = next(p for p in products if p.internal_id.startswith("آجیل خوری 11"))
    assert no_target.reference_price == 3_780_000
    assert "digikala2" not in no_target.platform_refs
    assert no_target.platform_refs["digikala"] == "20589157"
    assert no_target.target_price is None

    # spot-check a row where both columns are populated and differ -> proves
    # each column's own value goes to its own platform, never the other's.
    with_both = next(
        p for p in products if p.internal_id.startswith("سفره قلمکار سنتی 100*100")
    )
    assert with_both.platform_refs["digikala"] == "20597746"  # "دیجی کالا اصلی"
    assert with_both.platform_refs["digikala2"] == "22634773"  # "دیجی کالا سریع"

    # spot-check a row that DOES have "قیمت اصلی" populated. The sheet cell
    # is "1,800,000" Toman; Product.target_price must be the Rial value
    # (×10), per the Toman -> Rial conversion applied in rows_to_products.
    with_target = next(p for p in products if p.internal_id.startswith("سفره قلمکار اعلا 100*100 کد 01"))
    assert with_target.target_price == 18_000_000


def test_products_without_name_are_skipped():
    mapping = SheetMapping.load(MAPPING_PATH)
    source = CSVProductSource(FIXTURES / "sample_product_sheet.csv", mapping)
    products = source.fetch_products()
    assert all(p.internal_id.strip() for p in products)


def test_not_every_product_has_a_target_price_yet():
    # Documents real, expected state: only a subset of the catalog has
    # "قیمت اصلی" filled in. Products without it must surface as
    # FAILED_INVALID_DATA in the update service, never be silently skipped
    # or given an invented target.
    mapping = SheetMapping.load(MAPPING_PATH)
    source = CSVProductSource(FIXTURES / "sample_product_sheet.csv", mapping)
    products = source.fetch_products()
    with_target = [p for p in products if p.target_price is not None]
    assert 0 < len(with_target) < len(products)


# -- Toman -> Rial conversion (target price only) -----------------------------


def test_toman_to_rial_multiplies_by_10():
    assert toman_to_rial(1_000) == 10_000
    assert toman_to_rial(100_000) == 1_000_000


def test_rows_to_products_converts_target_price_toman_to_rial():
    header = ["اسم", "اسم جدید", "قیمت", "قیمت اصلی"]
    rows = [
        ["A", "A", "500,000", "1,000"],
        ["B", "B", "500,000", "100,000"],
    ]
    products = rows_to_products(header, rows, _TEST_MAPPING)

    assert products[0].target_price == 10_000  # 1,000 Toman -> 10,000 Rial
    assert products[1].target_price == 1_000_000  # 100,000 Toman -> 1,000,000 Rial
    # Reference price (from "قیمت", unused by DigiKala) is NOT converted —
    # only the target price is, per the explicit unit-correction scope.
    assert products[0].reference_price == 500_000


def test_rows_to_products_leaves_missing_target_price_as_none():
    header = ["اسم", "اسم جدید", "قیمت", "قیمت اصلی"]
    rows = [["A", "A", "500,000", ""]]
    products = rows_to_products(header, rows, _TEST_MAPPING)
    assert products[0].target_price is None


def test_12pct_rule_operates_correctly_on_the_converted_rial_value():
    # 200,000 Toman target -> 2,000,000 Rial. A reference price of
    # 1,000,000 Rial means the target is more than +12% over it, so the
    # rule must cap at the Rial scale (1,120,000), not the Toman scale.
    header = ["اسم", "اسم جدید", "قیمت", "قیمت اصلی"]
    rows = [["A", "A", "500,000", "200,000"]]
    product = rows_to_products(header, rows, _TEST_MAPPING)[0]
    assert product.target_price == 2_000_000

    decision = compute_price_to_apply(reference_price=1_000_000, target_price=product.target_price)
    assert decision.is_capped is True
    assert decision.price_to_apply == 1_120_000


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("3,780,000", 3_780_000),
        ("3780000", 3_780_000),
        ("20.000.000", 20_000_000),  # dots as thousands separators (was silently None)
        ("1.250.000", 1_250_000),
        ("۲۰.۰۰۰.۰۰۰", 20_000_000),  # Persian digits + dots
        ("3\u066c780\u066c000", 3_780_000),  # Persian thousands separator
        ("3\u060c780\u060c000", 3_780_000),  # Persian comma
        ("1.5", 1),  # a real decimal is NOT treated as a thousands separator
        ("", None),
        ("-", None),
        ("غ", None),
        (None, None),
    ],
)
def test_parse_price_handles_the_separators_used_in_the_sheet(raw, expected):
    assert _parse_price(raw) == expected


# -- Purple/red code-cell colors (status report: not counted as "not run yet") --


def _rgb(hex_color: str):
    return tuple(int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))


@pytest.mark.parametrize(
    "hex_color",
    ["#FF0000", "#F4CCCC", "#CC0000", "#E6B8AF", "#9900FF", "#D9D2E9", "#8E7CC3", "#20124D"],
)
def test_red_and_purple_shades_are_detected(hex_color):
    from faraz_honar.sheets.mapping import is_purple_or_red

    assert is_purple_or_red(_rgb(hex_color)) is True


@pytest.mark.parametrize(
    "hex_color",
    ["#FFFFFF", "#F3F3F3", "#000000", "#FF9900", "#FFFF00", "#00FF00", "#D9EAD3", "#0000FF", "#FCE5CD"],
)
def test_other_colors_are_not_detected(hex_color):
    from faraz_honar.sheets.mapping import is_purple_or_red

    assert is_purple_or_red(_rgb(hex_color)) is False


def test_no_color_is_not_detected():
    from faraz_honar.sheets.mapping import is_purple_or_red

    assert is_purple_or_red(None) is False


def test_rows_to_products_marks_platforms_with_purple_or_red_code_cell():
    from faraz_honar.sheets.mapping import SheetMapping, rows_to_products

    mapping = SheetMapping(
        name_column="اسم",
        display_name_column="اسم جدید",
        reference_price_column="قیمت",
        target_price_column=None,
        platform_ref_columns={"digikala": "دیجی کالا اصلی", "basalam": "باسلام"},
    )
    header = ["اسم", "اسم جدید", "قیمت", "دیجی کالا اصلی", "باسلام"]
    rows = [
        ["A", "A", "1", "111", "222"],  # digikala purple, basalam red
        ["B", "B", "1", "333", ""],  # digikala yellow; basalam has no code but is red
        ["C", "C", "1", "444", "555"],  # no colors at all
    ]
    purple, red, yellow = _rgb("#9900FF"), _rgb("#FF0000"), _rgb("#FFFF00")
    colors = [
        [None, None, None, purple, red],
        [None, None, None, yellow, red],
        [None, None, None, None, None],
    ]

    a, b, c = rows_to_products(header, rows, mapping, cell_colors=colors)

    assert a.color_excluded_platforms == frozenset({"digikala", "basalam"})
    assert b.color_excluded_platforms == frozenset()  # yellow, and an empty code cell is never "excluded"
    assert c.color_excluded_platforms == frozenset()


@pytest.mark.parametrize(
    "hex_color",
    ["#00FF00", "#D9EAD3", "#B6D7A8", "#93C47D", "#6AA84F", "#38761D", "#274E13", "#34A853", "#B7E1CD"],
)
def test_green_shades_are_detected(hex_color):
    from faraz_honar.sheets.mapping import is_green

    assert is_green(_rgb(hex_color)) is True


@pytest.mark.parametrize(
    "hex_color",
    ["#FFFFFF", "#F3F3F3", "#000000", "#FF0000", "#F4CCCC", "#FFFF00", "#FF9900", "#9900FF", "#0000FF", "#00FFFF"],
)
def test_non_green_colors_are_not_detected_as_green(hex_color):
    from faraz_honar.sheets.mapping import is_green

    assert is_green(_rgb(hex_color)) is False


def test_no_color_is_not_green():
    from faraz_honar.sheets.mapping import is_green

    assert is_green(None) is False


def _two_platform_mapping():
    from faraz_honar.sheets.mapping import SheetMapping

    return SheetMapping(
        name_column="اسم",
        display_name_column="اسم جدید",
        reference_price_column="قیمت",
        target_price_column="قیمت اصلی",
        platform_ref_columns={"digikala": "دیجی کالا اصلی", "basalam": "باسلام"},
    )


def test_rows_to_products_records_green_platforms_and_the_raw_new_name():
    from faraz_honar.sheets.mapping import rows_to_products

    header = ["اسم", "اسم جدید", "قیمت", "قیمت اصلی", "دیجی کالا اصلی", "باسلام"]
    rows = [
        ["A", "New A", "1", "100", "111", "222"],  # digikala green, basalam yellow
        ["B", "", "1", "100", "333", ""],  # no new name; basalam empty (green but no code)
        ["C", "New C", "1", "100", "444", "555"],  # no colors at all
    ]
    green, yellow = _rgb("#B6D7A8"), _rgb("#FFFF00")
    colors = [
        [None, None, None, None, green, yellow],
        [None, None, None, None, green, green],
        [None, None, None, None, None, None],
    ]

    a, b, c = rows_to_products(header, rows, _two_platform_mapping(), cell_colors=colors)

    assert a.green_platforms == frozenset({"digikala"})
    assert a.display_name == "New A"
    assert b.green_platforms == frozenset({"digikala"})  # an empty code cell is never green
    assert b.display_name is None  # `name` falls back to "اسم", the raw cell stays empty
    assert b.name == "B"
    assert c.green_platforms == frozenset()


def test_check_eligibility_needs_new_name_target_price_and_a_green_cell():
    from faraz_honar.sheets.mapping import rows_to_products

    header = ["اسم", "اسم جدید", "قیمت", "قیمت اصلی", "دیجی کالا اصلی", "باسلام"]
    rows = [
        ["ok", "New", "1", "100", "111", ""],
        ["no new name", "", "1", "100", "111", ""],
        ["no target", "New", "1", "", "111", ""],
        ["not green", "New", "1", "100", "111", ""],
        ["no code", "New", "1", "100", "", ""],
    ]
    green, red = _rgb("#B6D7A8"), _rgb("#FF0000")
    colors = [[None, None, None, None, green, None]] * 3 + [
        [None, None, None, None, red, None],
        [None, None, None, None, green, None],
    ]

    products = rows_to_products(header, rows, _two_platform_mapping(), cell_colors=colors)

    assert [p.is_check_eligible("digikala") for p in products] == [True, False, False, False, False]


def test_csv_without_colors_makes_nothing_check_eligible():
    from faraz_honar.sheets.mapping import rows_to_products

    header = ["اسم", "اسم جدید", "قیمت", "قیمت اصلی", "دیجی کالا اصلی", "باسلام"]
    (product,) = rows_to_products(header, [["A", "New", "1", "100", "111", ""]], _two_platform_mapping())

    assert product.green_platforms == frozenset()
    assert product.is_check_eligible("digikala") is False


def test_rows_to_products_without_colors_excludes_nothing():
    from faraz_honar.sheets.mapping import SheetMapping, rows_to_products

    mapping = SheetMapping(
        name_column="اسم",
        display_name_column="اسم جدید",
        reference_price_column="قیمت",
        target_price_column=None,
        platform_ref_columns={"digikala": "دیجی کالا اصلی"},
    )
    (product,) = rows_to_products(["اسم", "اسم جدید", "قیمت", "دیجی کالا اصلی"], [["A", "A", "1", "111"]], mapping)
    assert product.color_excluded_platforms == frozenset()
