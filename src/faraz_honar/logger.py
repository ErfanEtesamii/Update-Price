"""Persistent, rotating file logging for the Windows-service deployment.

Writes to ``logs/faraz-honar.log`` (relative to the working directory, which
NSSM sets to the project root via AppDirectory) and mirrors to stderr, which
NSSM captures separately in ``logs/service-stderr.log``.

Never raises: if the log file cannot be opened the app keeps running with
stderr-only logging instead of crashing the service.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DEFAULT_LOG_DIR = "logs"
DEFAULT_LOG_FILENAME = "faraz-honar.log"
MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
BACKUP_COUNT = 5  # faraz-honar.log + 5 rotated files


def setup_logging(
    log_dir: str | Path = DEFAULT_LOG_DIR,
    filename: str = DEFAULT_LOG_FILENAME,
    level: int = logging.INFO,
) -> None:
    # Persian text in log lines must not crash a service whose stderr is
    # redirected to a file with a legacy Windows code page.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    file_error: OSError | None = None
    try:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                path / filename,
                maxBytes=MAX_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
        )
    except OSError as exc:
        file_error = exc

    logging.basicConfig(level=level, format=LOG_FORMAT, handlers=handlers, force=True)

    # httpx (used by python-telegram-bot) logs every request URL at INFO, and
    # Telegram URLs contain the bot token: keep it out of the log files.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if file_error is not None:
        logging.getLogger(__name__).warning(
            "Could not open the log file in %s (%s); logging to stderr only.", log_dir, file_error
        )


def clear_log_file() -> bool:
    """Empties the running RotatingFileHandler's log file in place, for the
    "🧹 پاکسازی دیتابیس" control-panel action.

    The current log file is kept open by this same process the whole time
    the service runs. On Windows, an open file cannot be deleted/replaced
    from outside the process holding it, and even on Linux, unlinking it
    out from under the handler just leaves a dangling, invisible fd while a
    brand-new (empty) file never gets created in its place — so instead of
    removing the file, this truncates the handler's own stream to zero
    length, which is safe from any OS and any process.

    Previously rotated backups (faraz-honar.log.1 .. .BACKUP_COUNT) are NOT
    held open by anything, so those are safe to unlink normally.

    Returns True if a RotatingFileHandler was found and cleared, False if
    file logging isn't active (e.g. it failed to open at startup) — the
    caller should tell the user this part didn't happen.
    """
    for handler in logging.root.handlers:
        if isinstance(handler, RotatingFileHandler):
            handler.acquire()
            try:
                handler.stream.seek(0)
                handler.stream.truncate(0)
            finally:
                handler.release()

            log_path = Path(handler.baseFilename)
            for i in range(1, BACKUP_COUNT + 1):
                backup = log_path.with_name(f"{log_path.name}.{i}")
                backup.unlink(missing_ok=True)
            return True
    return False
