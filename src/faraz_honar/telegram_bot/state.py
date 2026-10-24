"""In-process run-lifecycle tracking for platform update jobs.

This is deliberately NOT the same thing as state/repository.py (which
persists per-product results to SQLite across process restarts). This
module only tracks "is a job running right now, and how far has it got" for
the lifetime of this process — exactly what's needed to:
  - show a live status/progress view in Telegram,
  - let the user request graceful cancellation, and
  - stop a manual Telegram-triggered run and the hourly recheck scheduler
    from ever processing the same platform at the same time.

Locking is a plain threading.Lock (not asyncio.Lock) because the actual
blocking work (PriceUpdateService.run()/recheck_capped_products()) always
runs in a worker thread via asyncio.to_thread — both the Telegram-triggered
run and the scheduled hourly recheck do this — so the mutual exclusion has
to hold across threads, not just within one coroutine.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from faraz_honar.core.exceptions import CrossPlatformStateError
from faraz_honar.core.models import OutcomeStatus, UpdateResult


@dataclass
class JobProgress:
    """Live progress for one in-flight run of one platform."""

    platform: str
    triggered_by: str
    total: int = 0
    processed: int = 0
    success: int = 0
    capped: int = 0
    failed: int = 0
    skipped: int = 0
    current_product_name: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    cancel_requested: bool = False
    # Error rounds (see PriceUpdateService._run_products): 1 = the normal first
    # pass; >1 = a re-run of only the products that failed transiently.
    round_no: int = 1
    round_total: int = 0
    # "update" = a normal price-update run (everything above); "check" = the
    # read-only "🔍 چک و بررسی قیمت‌ها" run, which reuses this object so the
    # status view, the Stop button and the per-platform lock work for it too.
    # For a check, `success` counts products that match the sheet, `mismatched`
    # those that don't, and `failed` those whose price could not be read.
    kind: str = "update"
    mismatched: int = 0

    def start_round(self, round_no: int, product_count: int) -> None:
        """Hook target for PriceUpdateService's on_round_start."""
        self.round_no = round_no
        self.round_total = product_count

    def record(self, result: UpdateResult) -> None:
        # Same isolation guarantee as RunSummary.add_result (core/models.py):
        # this JobProgress tracks exactly one platform's live progress, so a
        # result belonging to a different platform must never be counted
        # into it — that would show up as e.g. digikala2's progress counters
        # incrementing during a digikala run. Never caught/silenced.
        if result.platform != self.platform:
            raise CrossPlatformStateError(
                f"Refusing to record a '{result.platform}' result into the "
                f"'{self.platform}' JobProgress — progress must never cross "
                "platform boundaries."
            )
        if self.round_no > 1:
            # Error round: this result REPLACES that product's earlier
            # transient failure, which round 1 (or the previous round) already
            # counted in `processed` and `failed`. So `processed` must not go
            # up again, and the old failure is taken back out of `failed`;
            # the product is then counted once, under its newest outcome.
            # Safe because only transient failures are ever re-run.
            self.failed = max(0, self.failed - 1)
        else:
            self.processed += 1
        if result.status is OutcomeStatus.SUCCESS:
            self.success += 1
        elif result.status is OutcomeStatus.CAPPED_12PCT:
            self.capped += 1
        elif result.status is OutcomeStatus.SKIPPED_NO_CHANGE:
            self.skipped += 1
        elif result.is_failure:
            self.failed += 1

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at


@dataclass
class AllPlatformsBatch:
    """One in-flight "همه‌ی پلتفرم‌ها" run: the platforms still to go, in
    order, plus a cancel flag. Exists so the Stop button (and the
    single-batch guard) still work in the brief moments when no individual
    platform job is registered — between two platforms, or while waiting for
    a platform the hourly recheck is holding."""

    triggered_by: str
    remaining: list[str] = field(default_factory=list)
    current: str | None = None
    cancel_requested: bool = False


class PlatformJobManager:
    """Tracks active JobProgress(es) per platform name. Safe to call from
    any thread.

    By default this still enforces "exactly one active job per platform"
    (spec section 9) via try_start(). The one deliberate exception is
    start_parallel(): once a user has been shown the "another process is
    already running" warning and explicitly confirmed they want to run
    anyway (spec: parallel-run confirmation), a second (or third, ...)
    JobProgress can be registered for the same platform at the same time.
    Everything downstream (per-product state writes, Telegram reports) was
    already written per-run/per-JobProgress, so nothing else needs to know
    which mode created a given run.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()  # protects the dict below only
        self._jobs: dict[str, list[JobProgress]] = {}

    def try_start(self, platform: str, *, triggered_by: str) -> JobProgress | None:
        """Non-blocking. Returns a fresh JobProgress if no job for this
        platform is currently running (manual or scheduled), else None —
        exactly one active job per platform, whichever kind asked first.
        Use start_parallel() to deliberately bypass this after the user has
        confirmed running alongside an existing job.
        """
        with self._guard:
            if self._jobs.get(platform):
                return None
            progress = JobProgress(platform=platform, triggered_by=triggered_by)
            self._jobs[platform] = [progress]
            return progress

    def start_parallel(self, platform: str, *, triggered_by: str) -> JobProgress:
        """Always registers and returns a new JobProgress for `platform`,
        even if one or more jobs are already running for it. Only ever
        called after the user has been warned about the existing job and
        explicitly chosen to proceed anyway — never as a silent fallback
        when try_start() returns None.
        """
        progress = JobProgress(platform=platform, triggered_by=triggered_by)
        with self._guard:
            self._jobs.setdefault(platform, []).append(progress)
        return progress

    def finish(self, platform: str, progress: JobProgress | None = None) -> None:
        """Must be called exactly once for every successful try_start()/
        start_parallel(), normally from a `finally` block. When several
        jobs are running in parallel for the same platform, pass the exact
        `progress` object this call corresponds to so only that one is
        removed and the others keep running; omitting it (legacy/single-job
        callers) clears every job registered for the platform.
        """
        with self._guard:
            jobs = self._jobs.get(platform)
            if not jobs:
                return
            if progress is None:
                del self._jobs[platform]
            elif progress in jobs:
                jobs.remove(progress)
                if not jobs:
                    del self._jobs[platform]

    def request_cancel(self, platform: str) -> bool:
        """Returns True if at least one running job for this platform was
        signalled to stop (every parallel job for it, if more than one is
        active), False if nothing is currently running."""
        with self._guard:
            jobs = list(self._jobs.get(platform, []))
        if not jobs:
            return False
        for job in jobs:
            job.cancel_requested = True
        return True

    def current(self, platform: str) -> JobProgress | None:
        """The first active job for this platform, or None. When multiple
        jobs are running in parallel for it, use current_all() to see every
        one of them."""
        jobs = self.current_all(platform)
        return jobs[0] if jobs else None

    def current_all(self, platform: str) -> list[JobProgress]:
        with self._guard:
            return list(self._jobs.get(platform, []))

    def is_active(self, platform: str) -> bool:
        return bool(self.current_all(platform))
