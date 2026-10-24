import threading

from faraz_honar.core.models import OutcomeStatus, Product, UpdateResult
from faraz_honar.telegram_bot.state import JobProgress, PlatformJobManager


def _product(name="P") -> Product:
    return Product(internal_id=name, name=name, reference_price=1_000_000, target_price=1_100_000)


def _result(status: OutcomeStatus) -> UpdateResult:
    return UpdateResult(product=_product(), platform="digikala", status=status)


# -- JobProgress.record ---------------------------------------------------------


def test_job_progress_records_each_status_category():
    progress = JobProgress(platform="digikala", triggered_by="test")
    progress.record(_result(OutcomeStatus.SUCCESS))
    progress.record(_result(OutcomeStatus.CAPPED_12PCT))
    progress.record(_result(OutcomeStatus.SKIPPED_NO_CHANGE))
    progress.record(_result(OutcomeStatus.FAILED_API))

    assert progress.processed == 4
    assert progress.success == 1
    assert progress.capped == 1
    assert progress.skipped == 1
    assert progress.failed == 1


# -- Duplicate-run prevention ----------------------------------------------------


def test_try_start_returns_progress_when_free():
    manager = PlatformJobManager()
    progress = manager.try_start("digikala", triggered_by="telegram:1")
    assert progress is not None
    assert progress.platform == "digikala"
    assert progress.triggered_by == "telegram:1"


def test_try_start_returns_none_when_already_running():
    manager = PlatformJobManager()
    first = manager.try_start("digikala", triggered_by="telegram:1")
    assert first is not None

    second = manager.try_start("digikala", triggered_by="scheduler:hourly")
    assert second is None


def test_try_start_succeeds_again_after_finish():
    manager = PlatformJobManager()
    manager.try_start("digikala", triggered_by="telegram:1")
    manager.finish("digikala")

    again = manager.try_start("digikala", triggered_by="telegram:2")
    assert again is not None


def test_different_platforms_do_not_block_each_other():
    manager = PlatformJobManager()
    a = manager.try_start("digikala", triggered_by="telegram:1")
    b = manager.try_start("basalam", triggered_by="telegram:1")
    assert a is not None
    assert b is not None


def test_concurrent_try_start_only_one_wins():
    # Real multi-threaded race, matching how a manual run and the hourly
    # scheduler could actually both attempt to start at the same instant.
    manager = PlatformJobManager()
    winners: list[JobProgress] = []
    lock = threading.Lock()

    def attempt() -> None:
        progress = manager.try_start("digikala", triggered_by="race")
        if progress is not None:
            with lock:
                winners.append(progress)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1


# -- Cancellation -----------------------------------------------------------------


def test_request_cancel_on_running_job_sets_flag_and_returns_true():
    manager = PlatformJobManager()
    progress = manager.try_start("digikala", triggered_by="telegram:1")
    assert progress is not None

    assert manager.request_cancel("digikala") is True
    assert progress.cancel_requested is True


def test_request_cancel_when_nothing_running_returns_false():
    manager = PlatformJobManager()
    assert manager.request_cancel("digikala") is False


# -- current() for status reporting -----------------------------------------------


def test_current_is_none_when_nothing_running():
    manager = PlatformJobManager()
    assert manager.current("digikala") is None


# -- Parallel runs (start_parallel) -----------------------------------------------


def test_start_parallel_succeeds_even_while_a_job_is_already_running():
    manager = PlatformJobManager()
    manager.try_start("digikala", triggered_by="telegram:1")

    second = manager.start_parallel("digikala", triggered_by="telegram:2")

    assert second is not None
    assert len(manager.current_all("digikala")) == 2


def test_finish_with_explicit_progress_only_removes_that_one_job():
    manager = PlatformJobManager()
    first = manager.try_start("digikala", triggered_by="telegram:1")
    second = manager.start_parallel("digikala", triggered_by="telegram:2")

    manager.finish("digikala", second)

    assert manager.current_all("digikala") == [first]


def test_finish_without_progress_clears_every_job_for_the_platform():
    manager = PlatformJobManager()
    manager.try_start("digikala", triggered_by="telegram:1")
    manager.start_parallel("digikala", triggered_by="telegram:2")

    manager.finish("digikala")

    assert manager.current_all("digikala") == []


def test_request_cancel_signals_every_parallel_job():
    manager = PlatformJobManager()
    first = manager.try_start("digikala", triggered_by="telegram:1")
    second = manager.start_parallel("digikala", triggered_by="telegram:2")

    assert manager.request_cancel("digikala") is True
    assert first.cancel_requested is True
    assert second.cancel_requested is True


def test_is_active_reflects_whether_any_job_is_running():
    manager = PlatformJobManager()
    assert manager.is_active("digikala") is False
    manager.try_start("digikala", triggered_by="telegram:1")
    assert manager.is_active("digikala") is True


def test_current_reflects_live_progress():
    manager = PlatformJobManager()
    progress = manager.try_start("digikala", triggered_by="telegram:1")
    assert progress is not None
    progress.total = 10
    progress.current_product_name = "Widget"

    live = manager.current("digikala")
    assert live is progress
    assert live.total == 10
    assert live.current_product_name == "Widget"


# -- JobProgress in error rounds --------------------------------------------------


def _transient_failure(product: Product) -> UpdateResult:
    return UpdateResult(
        product=product, platform="digikala", status=OutcomeStatus.FAILED_API, transient=True
    )


def test_error_round_result_replaces_the_earlier_failure_instead_of_counting_twice():
    progress = JobProgress(platform="digikala", triggered_by="test")
    progress.total = 3
    a, b, c = _product("a"), _product("b"), _product("c")

    # Round 1: a succeeds, b and c fail transiently.
    progress.start_round(1, 3)
    progress.record(UpdateResult(product=a, platform="digikala", status=OutcomeStatus.SUCCESS))
    progress.record(_transient_failure(b))
    progress.record(_transient_failure(c))
    assert (progress.processed, progress.success, progress.failed) == (3, 1, 2)

    # Round 2: only b and c are retried; b now succeeds, c still fails.
    progress.start_round(2, 2)
    progress.record(UpdateResult(product=b, platform="digikala", status=OutcomeStatus.SUCCESS))
    progress.record(_transient_failure(c))

    assert progress.processed == 3  # never exceeds total
    assert progress.processed <= progress.total
    assert progress.success == 2
    assert progress.failed == 1
    assert (progress.round_no, progress.round_total) == (2, 2)


def test_error_round_failed_counter_never_goes_negative():
    progress = JobProgress(platform="digikala", triggered_by="test")
    progress.start_round(2, 1)
    progress.record(UpdateResult(product=_product(), platform="digikala", status=OutcomeStatus.SUCCESS))
    assert progress.failed == 0
