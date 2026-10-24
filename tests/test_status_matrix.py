"""Status-report matrix: which cells count as \"هنوز اجرا نشده\" and which are
gray/not-applicable -- in particular the purple/red code-cell exclusion."""
from faraz_honar.core.models import Product
from faraz_honar.reporting.status_matrix import CellStatus, build_status_matrix
from faraz_honar.reporting.xlsx_export import export_status_matrix_xlsx
from faraz_honar.state.repository import ProductStateRow

PLATFORMS = ["digikala", "basalam"]


def _product(name: str, refs: dict[str, str], excluded: frozenset[str] = frozenset()) -> Product:
    return Product(
        internal_id=name,
        name=name,
        reference_price=1_000,
        target_price=1_100,
        platform_refs=refs,
        color_excluded_platforms=excluded,
        display_name=name,
        green_platforms=frozenset(refs),
    )


def _state(product_id: str, platform: str, status: str) -> ProductStateRow:
    return ProductStateRow(
        product_id=product_id,
        platform=platform,
        product_name=product_id,
        reference_price=1_000,
        target_price=1_100,
        applied_price=None,
        is_capped=False,
        variant_id=None,
        status=status,
        retry_count=0,
        last_error=None,
        updated_at="2026-09-30T10:00:00",
    )


def test_code_present_no_state_is_not_run_yet():
    matrix = build_status_matrix([_product("a", {"digikala": "1"})], [], platforms=PLATFORMS)
    assert matrix.rows[0].cell("digikala").status is CellStatus.NOT_RUN_YET


def test_missing_code_is_not_applicable():
    matrix = build_status_matrix([_product("a", {})], [], platforms=PLATFORMS)
    assert matrix.rows[0].cell("digikala").status is CellStatus.NOT_APPLICABLE


def test_purple_or_red_code_without_state_is_not_counted_as_not_run_yet():
    product = _product("a", {"digikala": "1", "basalam": "2"}, excluded=frozenset({"digikala"}))
    matrix = build_status_matrix([product], [], platforms=PLATFORMS)
    row = matrix.rows[0]
    assert row.cell("digikala").status is CellStatus.NOT_APPLICABLE  # colored on digikala only
    assert row.cell("basalam").status is CellStatus.NOT_RUN_YET


def test_existing_state_wins_over_color_exclusion():
    product = _product("a", {"digikala": "1"}, excluded=frozenset({"digikala"}))
    matrix = build_status_matrix([product], [_state("a", "digikala", "success")], platforms=PLATFORMS)
    assert matrix.rows[0].cell("digikala").status is CellStatus.SUCCESS


def test_stale_failure_of_no_longer_eligible_product_is_ignored():
    # Failed in an earlier run, then its platform reference was removed from
    # the sheet: the run no longer touches it, so it must not stay "ناموفق".
    product = _product("a", {})
    matrix = build_status_matrix(
        [product], [_state("a", "digikala", "failed_missing_identifier")], platforms=PLATFORMS
    )
    assert matrix.rows[0].cell("digikala").status is CellStatus.NOT_APPLICABLE


def test_stale_failure_of_product_without_target_price_is_ignored():
    product = Product(
        internal_id="a", name="a", reference_price=1_000, target_price=None,
        platform_refs={"digikala": "1"},
    )
    matrix = build_status_matrix(
        [product], [_state("a", "digikala", "failed_api")], platforms=PLATFORMS
    )
    assert matrix.rows[0].cell("digikala").status is CellStatus.NOT_APPLICABLE


def test_summary_sheet_excludes_colored_products_from_not_run_and_eligible(tmp_path):
    from openpyxl import load_workbook

    products = [
        _product("p1", {"digikala": "1"}),  # not run yet
        _product("p2", {"digikala": "2"}, excluded=frozenset({"digikala"})),  # colored -> skipped
        _product("p3", {"digikala": "3"}),  # success
        _product("p4", {}),  # no code
    ]
    matrix = build_status_matrix(products, [_state("p3", "digikala", "success")], platforms=["digikala"])
    path = export_status_matrix_xlsx(matrix, tmp_path / "out.xlsx", include_reasons=False)

    rows = list(load_workbook(path)["خلاصه"].iter_rows(min_row=2, values_only=True))
    platform_row = rows[0]
    # (پلتفرم, واجد شرایط, موفق, ناموفق, هنوز اجرا نشده, درصد)
    assert platform_row[1:5] == (2, 1, 0, 1)


def _product_no_target(name: str, refs: dict[str, str]) -> Product:
    return Product(
        internal_id=name,
        name=name,
        reference_price=1_000,
        target_price=None,
        platform_refs=refs,
    )


def test_code_present_but_no_target_price_is_not_applicable():
    # The update run skips such a product (Product.is_eligible is False), so
    # it must not show up as "هنوز اجرا نشده" in the report either.
    matrix = build_status_matrix([_product_no_target("a", {"digikala": "1"})], [], platforms=PLATFORMS)
    assert matrix.rows[0].cell("digikala").status is CellStatus.NOT_APPLICABLE


def test_report_total_matches_run_eligible_count(tmp_path):
    """The summary's "تعداد کل واجد شرایط" equals what the run would process
    (service._is_eligible) when nothing is colored and no state exists yet."""
    from openpyxl import load_workbook

    from faraz_honar.update.service import _is_eligible

    products = [
        _product("p1", {"digikala": "1"}),
        _product("p2", {"digikala": "2"}),
        _product_no_target("p3", {"digikala": "3"}),  # link, no target price
        _product("p4", {}),  # no link
    ]
    run_total = sum(1 for p in products if _is_eligible(p, "digikala"))

    matrix = build_status_matrix(products, [], platforms=["digikala"])
    path = export_status_matrix_xlsx(matrix, tmp_path / "out.xlsx", include_reasons=False)
    rows = list(load_workbook(path)["خلاصه"].iter_rows(min_row=2, values_only=True))
    assert rows[0][1] == run_total == 2

def test_code_present_but_cell_not_green_is_not_applicable():
    # Only green cells are part of the run, so a non-green cell must not be
    # counted as "هنوز اجرا نشده" (nor keep a stale state row alive).
    product = Product(
        internal_id="a", name="a", reference_price=1_000, target_price=1_100,
        platform_refs={"digikala": "1"}, display_name="a",
    )
    matrix = build_status_matrix(
        [product], [_state("a", "digikala", "failed_api")], platforms=PLATFORMS
    )
    assert matrix.rows[0].cell("digikala").status is CellStatus.NOT_APPLICABLE


def test_code_present_but_no_display_name_is_not_applicable():
    product = Product(
        internal_id="a", name="a", reference_price=1_000, target_price=1_100,
        platform_refs={"digikala": "1"}, green_platforms=frozenset({"digikala"}),
    )
    matrix = build_status_matrix([product], [], platforms=PLATFORMS)
    assert matrix.rows[0].cell("digikala").status is CellStatus.NOT_APPLICABLE
