"""Read/write access to persisted product-update state (SQLite).

This is the single source of truth for "which products are still capped by
the 12% rule and need to be re-attempted on the next hourly run" (spec
section 8), and for retry/error history.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from faraz_honar.core.models import OutcomeStatus, RunSummary, UpdateResult
from faraz_honar.state.db import init_db, session


@dataclass
class ProductStateRow:
    product_id: str
    platform: str
    product_name: str | None
    reference_price: int | None
    target_price: int | None
    applied_price: int | None
    is_capped: bool
    variant_id: str | None
    status: str
    retry_count: int
    last_error: str | None
    updated_at: str


@dataclass
class RunReportRow:
    run_id: int
    platform: str
    triggered_by: str
    started_at: str
    finished_at: str | None
    results: list[ProductStateRow]


class StateRepository:
    def __init__(self, db_path: Path):
        self._db_path = db_path
        init_db(db_path)

    def record_result(self, result: UpdateResult) -> None:
        with session(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO product_state (
                    product_id, platform, product_name, reference_price, target_price,
                    applied_price, is_capped, variant_id, status, retry_count, last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id, platform) DO UPDATE SET
                    product_name=excluded.product_name,
                    reference_price=excluded.reference_price,
                    target_price=excluded.target_price,
                    applied_price=excluded.applied_price,
                    is_capped=excluded.is_capped,
                    variant_id=excluded.variant_id,
                    status=excluded.status,
                    retry_count=excluded.retry_count,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (
                    result.product.internal_id,
                    result.platform,
                    result.product.name,
                    result.previous_price,
                    result.target_price,
                    result.applied_price,
                    1 if result.is_capped else 0,
                    result.variant_id,
                    result.status.value,
                    result.attempts,
                    result.error_message,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def record_run(self, summary: RunSummary, *, started_at: datetime, finished_at: datetime) -> int:
        with session(self._db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO update_runs (
                    platform, triggered_by, started_at, finished_at,
                    success_count, capped_count, failed_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary.platform,
                    summary.triggered_by,
                    started_at.isoformat(),
                    finished_at.isoformat(),
                    len(summary.successful),
                    len(summary.capped),
                    len(summary.failed),
                ),
            )
            return int(cur.lastrowid)

    def start_run(self, *, platform: str, triggered_by: str, started_at: datetime) -> int:
        """Creates the update_runs row for a run before any products are processed,
        with zeroed counters and finished_at=NULL, and returns its id.

        Creating this row up front (instead of only recording the run once it is
        fully done, as record_run above does) means a run that gets stopped or
        crashes partway through still has a real run_id that record_run_product_result
        can attach to, and finish_run can later close out.
        """
        with session(self._db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO update_runs (
                    platform, triggered_by, started_at, finished_at,
                    success_count, capped_count, failed_count
                ) VALUES (?, ?, ?, NULL, 0, 0, 0)
                """,
                (platform, triggered_by, started_at.isoformat()),
            )
            return int(cur.lastrowid)

    def record_run_product_result(self, run_id: int, result: UpdateResult) -> None:
        """Inserts one row in run_product_results for this run/product.

        Fully independent of record_result above (which only ever updates the
        single latest-state row per product/platform in product_state, and is
        left untouched by this) -- this is an append-only per-run history used
        by the "last status" report.
        """
        with session(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO run_product_results (
                    run_id, product_id, product_name, reference_price, target_price,
                    applied_price, status, variant_id, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.product.internal_id,
                    result.product.name,
                    result.previous_price,
                    result.target_price,
                    result.applied_price,
                    result.status.value,
                    result.variant_id,
                    result.error_message,
                ),
            )

    def finish_run(self, run_id: int, *, summary: RunSummary, finished_at: datetime) -> None:
        """Sets finished_at and the final success/capped/failed counters on the
        update_runs row started by start_run (an UPDATE, not a new INSERT), then
        prunes older runs for that platform down to the last two.
        """
        with session(self._db_path) as conn:
            conn.execute(
                """
                UPDATE update_runs
                SET finished_at = ?, success_count = ?, capped_count = ?, failed_count = ?
                WHERE id = ?
                """,
                (
                    finished_at.isoformat(),
                    len(summary.successful),
                    len(summary.capped),
                    len(summary.failed),
                    run_id,
                ),
            )
        self.prune_old_runs(summary.platform)

    def prune_old_runs(self, platform: str, *, keep: int = 2) -> None:
        """Keeps only the `keep` most recent update_runs rows (by started_at) for
        this platform; deletes the rest. PRAGMA foreign_keys=ON (db.py) means the
        ON DELETE CASCADE on run_product_results.run_id cleans up their results too.
        """
        with session(self._db_path) as conn:
            conn.execute(
                """
                DELETE FROM update_runs
                WHERE platform = ? AND id NOT IN (
                    SELECT id FROM update_runs WHERE platform = ? ORDER BY started_at DESC LIMIT ?
                )
                """,
                (platform, platform, keep),
            )

    def list_recent_runs(self, platform: str, *, limit: int = 2) -> list[RunReportRow]:
        """Returns the `limit` most recent runs for this platform, newest first,
        each with its results populated from run_product_results.
        """
        with session(self._db_path) as conn:
            run_rows = conn.execute(
                "SELECT * FROM update_runs WHERE platform = ? ORDER BY started_at DESC LIMIT ?",
                (platform, limit),
            ).fetchall()
            reports = []
            for run_row in run_rows:
                result_rows = conn.execute(
                    "SELECT * FROM run_product_results WHERE run_id = ? ORDER BY id",
                    (run_row["id"],),
                ).fetchall()
                reports.append(
                    RunReportRow(
                        run_id=run_row["id"],
                        platform=run_row["platform"],
                        triggered_by=run_row["triggered_by"],
                        started_at=run_row["started_at"],
                        finished_at=run_row["finished_at"],
                        results=[_row_to_run_result(r) for r in result_rows],
                    )
                )
            return reports

    def get_last_run(self, platform: str) -> RunReportRow | None:
        """Returns the most recent run for this platform (with its per-product
        results populated), or None if this platform has no recorded run yet.

        Thin wrapper around list_recent_runs — no new SQL, just limit=1.
        """
        runs = self.list_recent_runs(platform, limit=1)
        return runs[0] if runs else None

    def list_all_product_states(self) -> list[ProductStateRow]:
        """Returns every row in product_state, across all platforms and
        regardless of is_capped -- unlike list_capped_products below, which
        filters to one platform's still-capped rows only.

        Used by the price-status Excel report (reporting/status_matrix.py),
        which needs the full (product_id, platform) picture to know which
        pairs have never been attempted at all, not just the capped subset.
        """
        with session(self._db_path) as conn:
            rows = conn.execute("SELECT * FROM product_state").fetchall()
            return [_row_to_state(r) for r in rows]

    def list_capped_products(self, platform: str) -> list[ProductStateRow]:
        with session(self._db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM product_state WHERE platform = ? AND is_capped = 1",
                (platform,),
            ).fetchall()
            return [_row_to_state(r) for r in rows]

    def get_product_state(self, product_id: str, platform: str) -> ProductStateRow | None:
        with session(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM product_state WHERE product_id = ? AND platform = ?",
                (product_id, platform),
            ).fetchone()
            return _row_to_state(row) if row else None

    def clear_all(self) -> None:
        """Wipes every persisted history table: run_product_results,
        update_runs, and product_state (spec: "🧹 پاکسازی دیتابیس").

        Explicit per-table DELETEs, all inside one session()/transaction —
        atomic, so either everything is gone or (on error) nothing is.
        run_product_results is deleted first purely for clarity (ON DELETE
        CASCADE from update_runs would clean it up on its own); deleting it
        explicitly is safer and more readable. Also resets the autoincrement
        counters so the next run_id starts back at 1.
        """
        with session(self._db_path) as conn:
            conn.execute("DELETE FROM run_product_results")
            conn.execute("DELETE FROM update_runs")
            conn.execute("DELETE FROM product_state")
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ('update_runs', 'run_product_results')"
            )

    def clear_capped_flag(self, product_id: str, platform: str) -> None:
        with session(self._db_path) as conn:
            conn.execute(
                "UPDATE product_state SET is_capped = 0, updated_at = ? "
                "WHERE product_id = ? AND platform = ?",
                (datetime.now(UTC).isoformat(), product_id, platform),
            )


def _row_to_run_result(row) -> ProductStateRow:
    """Maps a run_product_results row onto the same ProductStateRow shape used
    elsewhere, so reporting code (e.g. telegram_bot/reporting.py) can treat a
    run's per-product results the same way it treats product_state rows.

    platform, retry_count and updated_at aren't stored per-row in
    run_product_results (the platform lives on the parent RunReportRow instead,
    and retry_count/updated_at only make sense for product_state), so they're
    filled with harmless defaults rather than left unset.
    """
    return ProductStateRow(
        product_id=row["product_id"],
        platform="",
        product_name=row["product_name"],
        reference_price=row["reference_price"],
        target_price=row["target_price"],
        applied_price=row["applied_price"],
        is_capped=row["status"] == OutcomeStatus.CAPPED_12PCT.value,
        variant_id=row["variant_id"],
        status=row["status"],
        retry_count=0,
        last_error=row["last_error"],
        updated_at="",
    )


def _row_to_state(row) -> ProductStateRow:
    return ProductStateRow(
        product_id=row["product_id"],
        platform=row["platform"],
        product_name=row["product_name"],
        reference_price=row["reference_price"],
        target_price=row["target_price"],
        applied_price=row["applied_price"],
        is_capped=bool(row["is_capped"]),
        variant_id=row["variant_id"],
        status=row["status"],
        retry_count=row["retry_count"],
        last_error=row["last_error"],
        updated_at=row["updated_at"],
    )


__all__ = ["StateRepository", "ProductStateRow", "RunReportRow", "OutcomeStatus"]