-- Persistent state for the price-update pipeline.
-- One row per (product, platform): tracks the latest known outcome so the
-- hourly job can find products still capped by the 12% rule (spec section 8)
-- and so retries/reporting have history to refer back to.

CREATE TABLE IF NOT EXISTS product_state (
    product_id       TEXT    NOT NULL,
    platform         TEXT    NOT NULL,
    product_name     TEXT,
    reference_price  INTEGER,
    target_price     INTEGER,
    applied_price    INTEGER,
    is_capped        INTEGER NOT NULL DEFAULT 0,
    variant_id       TEXT,
    status           TEXT    NOT NULL,
    retry_count      INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    updated_at       TEXT    NOT NULL,
    PRIMARY KEY (product_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_product_state_capped
    ON product_state (platform, is_capped);

CREATE TABLE IF NOT EXISTS update_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT    NOT NULL,
    triggered_by    TEXT    NOT NULL,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    success_count   INTEGER NOT NULL DEFAULT 0,
    capped_count    INTEGER NOT NULL DEFAULT 0,
    failed_count    INTEGER NOT NULL DEFAULT 0
);

-- Per-run, per-product results for the Telegram "last status" report.
-- Independent of product_state (which only tracks the latest state per
-- product/platform for the hourly capped-retry job) -- this table keeps a
-- full snapshot of each run so the last two runs per platform can be shown
-- in full, including runs that ended up capped or failed.
CREATE TABLE IF NOT EXISTS run_product_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES update_runs(id) ON DELETE CASCADE,
    product_id       TEXT    NOT NULL,
    product_name     TEXT,
    reference_price  INTEGER,
    target_price     INTEGER,
    applied_price    INTEGER,
    status           TEXT    NOT NULL,
    variant_id       TEXT,
    last_error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_product_results_run
    ON run_product_results (run_id);
