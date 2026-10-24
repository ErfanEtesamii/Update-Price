import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from faraz_honar.core.models import OutcomeStatus, Product, RunSummary, UpdateResult
from faraz_honar.state import db as state_db
from faraz_honar.state.repository import StateRepository


def _product() -> Product:
    return Product(internal_id="p1", name="Test Product", reference_price=1000, target_price=1500)


def test_record_and_read_capped_product(tmp_path: Path):
    repo = StateRepository(tmp_path / "state.db")
    result = UpdateResult(
        product=_product(),
        platform="digikala",
        status=OutcomeStatus.CAPPED_12PCT,
        previous_price=1000,
        applied_price=1120,
        target_price=1500,
        attempts=1,
        variant_id="V1",
    )
    repo.record_result(result)

    capped = repo.list_capped_products("digikala")
    assert len(capped) == 1
    assert capped[0].product_id == "p1"
    assert capped[0].applied_price == 1120
    assert capped[0].is_capped is True

    state = repo.get_product_state("p1", "digikala")
    assert state is not None
    assert state.status == "capped_12pct"


def test_clear_capped_flag(tmp_path: Path):
    repo = StateRepository(tmp_path / "state.db")
    result = UpdateResult(
        product=_product(),
        platform="digikala",
        status=OutcomeStatus.CAPPED_12PCT,
        applied_price=1120,
        target_price=1500,
        attempts=1,
    )
    repo.record_result(result)
    assert len(repo.list_capped_products("digikala")) == 1

    repo.clear_capped_flag("p1", "digikala")
    assert len(repo.list_capped_products("digikala")) == 0


def test_record_run_summary(tmp_path: Path):
    repo = StateRepository(tmp_path / "state.db")
    summary = RunSummary(platform="digikala", triggered_by="telegram:123")
    summary.results.append(
        UpdateResult(product=_product(), platform="digikala", status=OutcomeStatus.SUCCESS)
    )
    run_id = repo.record_run(summary, started_at=datetime.now(UTC), finished_at=datetime.now(UTC))
    assert run_id >= 1


# -- One-off migration: DigiKala platform keys were renamed -----------------------
#
# Before the rename the DB stored the FAST account as platform "digikala" and
# the MAIN account as "digikala_asli". They are now "digikala2" and "digikala".


def _legacy_db(path: Path) -> None:
    """A database file exactly as the old code left it (user_version 0)."""
    conn = sqlite3.connect(path)
    conn.executescript(state_db._SCHEMA_PATH.read_text(encoding="utf-8"))  # noqa: SLF001

    def add_state(product_id: str, platform: str, *, capped: int = 0) -> None:
        conn.execute(
            "INSERT INTO product_state (product_id, platform, status, is_capped, updated_at) "
            "VALUES (?, ?, 'capped_12pct', ?, 't')",
            (product_id, platform, capped),
        )

    add_state("P1", "digikala", capped=1)  # old fast account
    add_state("P1", "digikala_asli", capped=0)  # old main account
    add_state("P2", "digikala", capped=1)
    add_state("P3", "basalam")
    conn.execute(
        "INSERT INTO update_runs (platform, triggered_by, started_at) VALUES ('digikala', 'x', 't')"
    )
    conn.execute(
        "INSERT INTO update_runs (platform, triggered_by, started_at) VALUES ('digikala_asli', 'x', 't')"
    )
    conn.commit()
    conn.close()


def test_migration_swaps_old_digikala_keys_to_the_new_names(tmp_path: Path):
    db_path = tmp_path / "state.db"
    _legacy_db(db_path)

    repo = StateRepository(db_path)

    # Old "digikala" (fast) rows are now "digikala2"; old "digikala_asli" is now "digikala".
    assert {r.product_id for r in repo.list_capped_products("digikala2")} == {"P1", "P2"}
    assert repo.list_capped_products("digikala") == []  # the old main account had nothing capped
    assert repo.get_product_state("P1", "digikala") is not None
    assert repo.get_product_state("P2", "digikala") is None
    assert repo.get_product_state("P3", "basalam") is not None  # other platforms untouched

    conn = sqlite3.connect(db_path)
    runs = sorted(r[0] for r in conn.execute("SELECT platform FROM update_runs"))
    assert runs == ["digikala", "digikala2"]
    assert conn.execute("SELECT COUNT(*) FROM product_state WHERE platform = 'digikala_asli'").fetchone()[0] == 0
    conn.close()


def test_migration_runs_only_once_per_database_file(tmp_path: Path):
    db_path = tmp_path / "state.db"
    _legacy_db(db_path)

    StateRepository(db_path)  # first start after the upgrade: swaps
    StateRepository(db_path)  # every later start: must NOT swap back or scramble again
    StateRepository(db_path)

    repo = StateRepository(db_path)
    assert {r.product_id for r in repo.list_capped_products("digikala2")} == {"P1", "P2"}
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()


def test_migration_is_a_no_op_for_a_brand_new_database(tmp_path: Path):
    db_path = tmp_path / "fresh.db"
    repo = StateRepository(db_path)
    repo.record_result(
        UpdateResult(
            product=_product(), platform="digikala", status=OutcomeStatus.CAPPED_12PCT,
            previous_price=1000, applied_price=1120, target_price=1500, variant_id="V1",
        )
    )

    # Reopening (which re-runs init) must not touch rows written by the new code.
    reopened = StateRepository(db_path)
    assert len(reopened.list_capped_products("digikala")) == 1
    assert reopened.list_capped_products("digikala2") == []


# -- "Last status" run history: start_run / record_run_product_result /
# finish_run / list_recent_runs / prune_old_runs ---------------------------


def _result(name: str = "Widget", *, status: OutcomeStatus = OutcomeStatus.SUCCESS) -> UpdateResult:
    return UpdateResult(
        product=Product(internal_id=name, name=name, reference_price=1000, target_price=1200),
        platform="digikala",
        status=status,
        previous_price=1000,
        applied_price=1150,
        target_price=1200,
        variant_id="V1",
    )


def test_start_run_record_results_finish_run_round_trip(tmp_path: Path):
    repo = StateRepository(tmp_path / "state.db")
    started = datetime.now(UTC)

    run_id = repo.start_run(platform="digikala", triggered_by="telegram:1", started_at=started)
    assert run_id >= 1

    r1 = _result("Widget A")
    r2 = _result("Widget B", status=OutcomeStatus.CAPPED_12PCT)
    repo.record_run_product_result(run_id, r1)
    repo.record_run_product_result(run_id, r2)

    summary = RunSummary(platform="digikala", triggered_by="telegram:1")
    summary.add_result(r1)
    summary.add_result(r2)
    repo.finish_run(run_id, summary=summary, finished_at=datetime.now(UTC))

    [run] = repo.list_recent_runs("digikala", limit=1)
    assert run.run_id == run_id
    assert run.finished_at is not None
    assert [r.product_name for r in run.results] == ["Widget A", "Widget B"]
    assert run.results[1].status == OutcomeStatus.CAPPED_12PCT.value


def test_finish_run_marks_a_stopped_run_as_incomplete(tmp_path: Path):
    # A run stopped mid-way should still end up with finished_at set (via
    # the try/finally in update.service._run_products) and only the
    # products actually processed before the stop.
    repo = StateRepository(tmp_path / "state.db")
    run_id = repo.start_run(platform="digikala", triggered_by="telegram:1", started_at=datetime.now(UTC))
    r1 = _result("Only processed one")
    repo.record_run_product_result(run_id, r1)

    summary = RunSummary(platform="digikala", triggered_by="telegram:1")
    summary.add_result(r1)
    repo.finish_run(run_id, summary=summary, finished_at=datetime.now(UTC))

    [run] = repo.list_recent_runs("digikala", limit=1)
    assert run.finished_at is not None
    assert len(run.results) == 1


def test_list_recent_runs_orders_newest_first(tmp_path: Path):
    repo = StateRepository(tmp_path / "state.db")
    older = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    newer = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)

    run1 = repo.start_run(platform="digikala", triggered_by="t", started_at=older)
    repo.finish_run(run1, summary=RunSummary(platform="digikala", triggered_by="t"), finished_at=older)
    run2 = repo.start_run(platform="digikala", triggered_by="t", started_at=newer)
    repo.finish_run(run2, summary=RunSummary(platform="digikala", triggered_by="t"), finished_at=newer)

    runs = repo.list_recent_runs("digikala", limit=2)
    assert [r.run_id for r in runs] == [run2, run1]


def test_prune_old_runs_keeps_only_the_last_two_per_platform(tmp_path: Path):
    repo = StateRepository(tmp_path / "state.db")
    run_ids = []
    for i in range(3):
        started = datetime(2026, 9, 20 + i, 10, 0, tzinfo=UTC)
        run_id = repo.start_run(platform="digikala", triggered_by="t", started_at=started)
        run_ids.append(run_id)
        repo.record_run_product_result(run_id, _result(f"Widget {i}"))
        repo.finish_run(
            run_id, summary=RunSummary(platform="digikala", triggered_by="t"), finished_at=started
        )

    runs = repo.list_recent_runs("digikala", limit=10)
    assert {r.run_id for r in runs} == {run_ids[1], run_ids[2]}

    # CASCADE: run_product_results for the pruned oldest run must be gone too.
    conn = sqlite3.connect(tmp_path / "state.db")
    remaining_run_ids = {row[0] for row in conn.execute("SELECT run_id FROM run_product_results")}
    conn.close()
    assert run_ids[0] not in remaining_run_ids


def test_list_recent_runs_isolates_platforms(tmp_path: Path):
    repo = StateRepository(tmp_path / "state.db")
    d_run = repo.start_run(platform="digikala", triggered_by="t", started_at=datetime.now(UTC))
    b_run = repo.start_run(platform="basalam", triggered_by="t", started_at=datetime.now(UTC))
    repo.record_run_product_result(d_run, _result("Digi Widget"))
    repo.finish_run(d_run, summary=RunSummary(platform="digikala", triggered_by="t"), finished_at=datetime.now(UTC))
    repo.finish_run(b_run, summary=RunSummary(platform="basalam", triggered_by="t"), finished_at=datetime.now(UTC))

    [digi] = repo.list_recent_runs("digikala", limit=10)
    [basalam] = repo.list_recent_runs("basalam", limit=10)
    assert digi.results[0].product_name == "Digi Widget"
    assert basalam.results == []


def test_list_recent_runs_for_a_platform_with_no_runs_yet(tmp_path: Path):
    repo = StateRepository(tmp_path / "state.db")
    assert repo.list_recent_runs("digikala", limit=2) == []


# -- get_last_run (spec section 3: "retry failed only" confirmation) --------


def test_get_last_run_for_a_platform_with_no_runs_yet(tmp_path: Path):
    repo = StateRepository(tmp_path / "state.db")
    assert repo.get_last_run("digikala") is None


def test_get_last_run_returns_the_only_run(tmp_path: Path):
    repo = StateRepository(tmp_path / "state.db")
    started = datetime.now(UTC)
    run_id = repo.start_run(platform="digikala", triggered_by="telegram:1", started_at=started)
    r1 = _result("Widget A", status=OutcomeStatus.FAILED_API)
    repo.record_run_product_result(run_id, r1)
    summary = RunSummary(platform="digikala", triggered_by="telegram:1")
    summary.add_result(r1)
    repo.finish_run(run_id, summary=summary, finished_at=datetime.now(UTC))

    run = repo.get_last_run("digikala")
    assert run is not None
    assert run.run_id == run_id
    assert [r.product_name for r in run.results] == ["Widget A"]


def test_get_last_run_returns_the_most_recent_of_several(tmp_path: Path):
    repo = StateRepository(tmp_path / "state.db")
    older = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    newer = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)

    run1 = repo.start_run(platform="digikala", triggered_by="t", started_at=older)
    repo.finish_run(run1, summary=RunSummary(platform="digikala", triggered_by="t"), finished_at=older)
    run2 = repo.start_run(platform="digikala", triggered_by="t", started_at=newer)
    repo.finish_run(run2, summary=RunSummary(platform="digikala", triggered_by="t"), finished_at=newer)

    run = repo.get_last_run("digikala")
    assert run is not None
    assert run.run_id == run2
