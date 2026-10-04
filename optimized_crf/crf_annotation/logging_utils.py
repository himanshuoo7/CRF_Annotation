"""Logging setup.

`configure_logging` is idempotent and safe to call more than once: it tags the
handlers it installs and replaces them, so repeated calls in one interpreter —
a worker process, a test suite — do not duplicate every record.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

LOGGER_NAME = "SDTM-CRF-ANNOTATION"
# A long-lived worker writes to one log file indefinitely; cap it so a single
# runaway study cannot fill the volume.
MAX_LOG_BYTES = 20 * 1024 * 1024
LOG_BACKUPS = 5
_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_HANDLER_TAG = "_acrf_handler"

_current_log_file: Optional[Path] = None
_configured_dir: Optional[Path] = None


def configure_logging(log_dir: Optional[os.PathLike | str], level: int = logging.INFO) -> Optional[Path]:
    """Attach console + file handlers to the root logger exactly once.

    Returns the log file path, or None when no directory was given.
    """
    global _current_log_file, _configured_dir

    requested = Path(log_dir).expanduser() if log_dir else None
    if requested is not None and requested == _configured_dir:
        return _current_log_file  # already wired up for this directory

    root = logging.getLogger()
    root.setLevel(level)

    # Drop handlers we installed on a previous call so repeated invocations
    # inside one interpreter (a worker process, a test suite) do not duplicate.
    for handler in [h for h in root.handlers if getattr(h, _HANDLER_TAG, False)]:
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(level)
    setattr(console, _HANDLER_TAG, True)
    root.addHandler(console)

    if requested is None:
        _current_log_file = None
        _configured_dir = None
        return None

    from .constants import LOG_FILE_NAME

    directory = requested
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / LOG_FILE_NAME

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, mode="a", encoding="utf-8",
        maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS,
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    setattr(file_handler, _HANDLER_TAG, True)
    root.addHandler(file_handler)

    _current_log_file = log_file
    _configured_dir = directory
    logger = get_logger()
    logger.info("=" * 50)
    logger.info("SDTM CRF annotation run started at %s", datetime.now().isoformat(timespec="seconds"))
    return log_file


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)


def current_log_file() -> Optional[Path]:
    return _current_log_file
