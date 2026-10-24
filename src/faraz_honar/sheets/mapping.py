"""Turns raw header/row text (from CSV or the Sheets API) into Products,
using the column mapping in config/sheet_mapping.yaml.

Target prices are converted from the sheet's Toman to Rial here (see
toman_to_rial) — the sheet is the only place this unit correction is
needed; everything downstream already expects Rial.
"""
from __future__ import annotations

import colorsys
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from faraz_honar.core.exceptions import ConfigurationError
from faraz_honar.core.models import Product

_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_DIGIT_MAP = {d: str(i) for i, d in enumerate(_PERSIAN_DIGITS)}
_DIGIT_MAP.update({d: str(i) for i, d in enumerate(_ARABIC_DIGITS)})
_DIGIT_TRANSLATION = str.maketrans(_DIGIT_MAP)


@dataclass(frozen=True)
class SheetMapping:
    name_column: str
    display_name_column: str
    reference_price_column: str
    target_price_column: str | None
    platform_ref_columns: dict[str, str] = field(default_factory=dict)
    note_column: str | None = None

    @classmethod
    def load(cls, path: Path) -> SheetMapping:
        if not path.exists():
            raise ConfigurationError(f"Sheet mapping config not found: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        try:
            return cls(
                name_column=raw["name_column"],
                display_name_column=raw["display_name_column"],
                reference_price_column=raw["reference_price_column"],
                target_price_column=raw.get("target_price_column"),
                platform_ref_columns=raw.get("platform_ref_columns", {}),
                note_column=raw.get("note_column"),
            )
        except KeyError as exc:
            raise ConfigurationError(f"Sheet mapping config missing required key: {exc}") from exc


def _normalize_header(text: str) -> str:
    return " ".join(text.split())


# "20.000.000" (dots as thousands separators) used to fail int(float(...)) and
# the product was then silently treated as having NO target price, so its price
# was never updated and nothing was reported. Only a dot that is clearly a
# thousands separator (groups of exactly 3 digits) is stripped; a real decimal
# such as "1.5" is left alone. The Persian thousands separator "٬" and the
# Persian comma "،" are handled like ",".
_DOT_THOUSANDS = re.compile(r"^\d{1,3}(\.\d{3})+$")


def _parse_price(raw: str | None) -> int | None:
    if raw is None:
        return None
    text = raw.translate(_DIGIT_TRANSLATION).replace(",", "").replace("\u066c", "").replace("\u060c", "")
    text = text.strip()
    if not text:
        return None
    if _DOT_THOUSANDS.match(text):
        text = text.replace(".", "")
    try:
        return int(float(text))
    except ValueError:
        return None


_TOMAN_TO_RIAL = 10


def toman_to_rial(toman: int) -> int:
    """Google Sheet prices are quoted in Toman; DigiKala's API and the rest
    of the pricing pipeline operate in Rial (1 Toman = 10 Rial)."""
    return toman * _TOMAN_TO_RIAL


# (red, green, blue), each 0.0-1.0 -- the same scale the Google Sheets API
# uses for Color objects.
RGB = tuple[float, float, float]

# Hue windows (degrees) for the two "skip this platform" marker colors. They
# cover Google Sheets' whole red family (light red 3 ... dark red 3, plus the
# red-berry shades) and purple family (light purple 3 ... dark purple 3, plus
# magenta). Orange (~30°), yellow, green, blue and pink-ish tints outside
# these windows are not matched.
_RED_HUE_MAX = 12.0
_RED_HUE_MIN = 348.0
_PURPLE_HUE_MIN = 245.0
_PURPLE_HUE_MAX = 300.0
# Below this saturation a fill is white/grey for our purposes (Google's
# lightest tints, e.g. light purple 3 #D9D2E9, still sit around 0.10);
# below this value it is black.
_MIN_SATURATION = 0.08
_MIN_VALUE = 0.25
# Green family: Google Sheets' light green 3 (#D9EAD3, ~104°) through dark
# green 3 (#274E13, ~100°), pure green (#00FF00, 120°) and green (#34A853,
# ~137°). Yellow (~60°) and cyan/teal (~180°) are outside the window.
_GREEN_HUE_MIN = 70.0
_GREEN_HUE_MAX = 170.0


def is_purple_or_red(rgb: RGB | None) -> bool:
    """True when a cell background color is in the red or purple family.

    Works on hue, not on exact palette values, so every shade of the two
    colors (light/dark variants) is matched while white, grey, black and
    other colors are not.
    """
    if rgb is None:
        return False
    hue, saturation, value = colorsys.rgb_to_hsv(*rgb)
    if saturation < _MIN_SATURATION or value < _MIN_VALUE:
        return False
    degrees = hue * 360.0
    if degrees <= _RED_HUE_MAX or degrees >= _RED_HUE_MIN:
        return True
    return _PURPLE_HUE_MIN <= degrees <= _PURPLE_HUE_MAX


def is_green(rgb: RGB | None) -> bool:
    """True when a cell background color is in the green family (any shade;
    white, grey, black and other hues are not)."""
    if rgb is None:
        return False
    hue, saturation, value = colorsys.rgb_to_hsv(*rgb)
    if saturation < _MIN_SATURATION or value < _MIN_VALUE:
        return False
    return _GREEN_HUE_MIN <= hue * 360.0 <= _GREEN_HUE_MAX


class HeaderNotFoundError(ConfigurationError):
    pass


def _index_of(header: list[str], wanted: str, *, required: bool) -> int | None:
    normalized_wanted = _normalize_header(wanted)
    for i, h in enumerate(header):
        if _normalize_header(h) == normalized_wanted:
            return i
    if required:
        raise HeaderNotFoundError(
            f"Column '{wanted}' not found in sheet header. "
            f"Header row was: {header!r}. Update config/sheet_mapping.yaml if the "
            f"sheet's columns changed."
        )
    return None


def rows_to_products(
    header: list[str],
    rows: list[list[str]],
    mapping: SheetMapping,
    cell_colors: list[list[RGB | None]] | None = None,
) -> list[Product]:
    """Convert raw header + data rows into Product objects.

    Rows with an empty name column are skipped (blank spacer rows, as seen
    at the top of the sample export).

    `cell_colors`, when given, is parallel to `rows` (same row and column
    positions, background color per cell or None). A platform whose code cell
    has a purple/red background is recorded in
    Product.color_excluded_platforms. Omitted for CSV sources, which have no
    color information.
    """
    name_idx = _index_of(header, mapping.name_column, required=True)
    display_idx = _index_of(header, mapping.display_name_column, required=True)
    ref_price_idx = _index_of(header, mapping.reference_price_column, required=True)
    target_price_idx = (
        _index_of(header, mapping.target_price_column, required=False)
        if mapping.target_price_column
        else None
    )
    note_idx = _index_of(header, mapping.note_column, required=False) if mapping.note_column else None

    platform_indices: dict[str, int | None] = {
        platform: _index_of(header, col, required=False)
        for platform, col in mapping.platform_ref_columns.items()
    }

    def cell(row: list[str], idx: int | None) -> str | None:
        if idx is None or idx >= len(row):
            return None
        value = row[idx].strip()
        return value or None

    products: list[Product] = []
    for row_idx, row in enumerate(rows):
        # Some rows only have the cleaned display name ("اسم جدید") and an
        # empty "اسم" cell. Fall back to it so those rows are not mistaken
        # for blank spacer rows; skip only when BOTH are empty.
        display_name = cell(row, display_idx)
        name = cell(row, name_idx) or display_name
        if not name:
            continue  # blank spacer row

        reference_price = _parse_price(cell(row, ref_price_idx))
        # Converted here, at the point the sheet's target-price cell is
        # parsed, so every downstream consumer of Product.target_price
        # (pricing, the DigiKala update, and the hourly recheck's fresh
        # re-fetch of this same function) already sees Rial and needs no
        # unit-awareness of its own.
        target_price_toman = (
            _parse_price(cell(row, target_price_idx)) if target_price_idx is not None else None
        )
        target_price = toman_to_rial(target_price_toman) if target_price_toman is not None else None

        platform_refs = {
            platform: value
            for platform, idx in platform_indices.items()
            if (value := cell(row, idx)) is not None
        }

        row_colors = cell_colors[row_idx] if cell_colors and row_idx < len(cell_colors) else []
        color_excluded = frozenset(
            platform
            for platform, idx in platform_indices.items()
            # Only a cell that actually holds a code can be "excluded"; an
            # empty code cell is already not-applicable.
            if platform in platform_refs
            and idx is not None
            and idx < len(row_colors)
            and is_purple_or_red(row_colors[idx])
        )

        green = frozenset(
            platform
            for platform, idx in platform_indices.items()
            if platform in platform_refs
            and idx is not None
            and idx < len(row_colors)
            and is_green(row_colors[idx])
        )

        raw = {
            _normalize_header(header[i]): row[i]
            for i in range(min(len(header), len(row)))
            if row[i].strip()
        }
        if note_idx is not None:
            raw.setdefault("_note", cell(row, note_idx) or "")

        products.append(
            Product(
                internal_id=name,
                name=display_name or name,
                reference_price=reference_price,
                target_price=target_price,
                platform_refs=platform_refs,
                raw=raw,
                color_excluded_platforms=color_excluded,
                display_name=display_name,
                green_platforms=green,
            )
        )
    return products