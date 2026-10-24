from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# PRAGMA user_version marks which one-off data migrations a database file has
# already had, so each runs exactly once per file.
#   0 -> 1: the DigiKala platform keys were renamed.
_LATEST_USER_VERSION = 1

# Order matters: each step frees the name the next step needs.
_DIGIKALA_KEY_RENAME_STEPS = (
    ("digikala", "__old_digikala_fast"),  # old fast account (name is taken by the main one next)
    ("digikala_asli", "digikala"),  # old main account -> "digikala"
    ("__old_digikala_fast", "digikala2"),  # old fast account -> "digikala2"
)


def _migrate_digikala_platform_keys(conn: sqlite3.Connection) -> None:
    """Before this change the DB stored the fast DigiKala account as
    platform='digikala' and the main one as platform='digikala_asli'. They are
    now 'digikala2' and 'digikala' respectively. Without this swap, existing
    rows would silently point at the WRONG account (e.g. the fast account's
    capped-product history read back as the main account's).

    Runs in one transaction and only once per database file (user_version).
    """
    for table in ("product_state", "update_runs"):
        for old, new in _DIGIKALA_KEY_RENAME_STEPS:
            conn.execute(f"UPDATE {table} SET platform = ? WHERE platform = ?", (new, old))  # noqa: S608


def _run_migrations(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= _LATEST_USER_VERSION:
        return
    with conn:  # single transaction: either the whole swap + version bump lands, or none of it
        if version < 1:
            _migrate_digikala_platform_keys(conn)
        conn.execute(f"PRAGMA user_version = {_LATEST_USER_VERSION}")


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
        _run_migrations(conn)
    finally:
        conn.close()


@contextmanager
def session(db_path: Path):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
