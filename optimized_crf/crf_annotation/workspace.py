"""Scratch space for a single run.

Used as a context manager, so the temp directory is removed on every exit path
including exceptions and `KeyboardInterrupt`. The old FileManager only cleaned
up inside `save_output_files`, so any failure before that point left a
`temp_<timestamp>/` directory behind in the *input* directory forever.
"""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Optional

try:  # POSIX advisory locking; absent on Windows
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]

from .constants import OUTPUT_CONFIG_NAME, OUTPUT_PDF_NAME
from .logging_utils import get_logger

logger = get_logger()


@dataclass
class OutputFiles:
    pdf_path: Optional[Path] = None
    config_path: Optional[Path] = None

    def __bool__(self) -> bool:
        return self.pdf_path is not None or self.config_path is not None


class ConcurrentRunError(RuntimeError):
    """Another run already holds this target directory."""


class Workspace:
    """Temp working directory, an exclusive lock, and the final publish step."""

    LOCK_NAME = ".acrf.lock"

    def __init__(self, target_path: Path, lock: bool = True) -> None:
        self.target_path = Path(target_path)
        self.target_path.mkdir(parents=True, exist_ok=True)
        # Scratch lives under the target (guaranteed writable, and the run
        # already owns it) rather than polluting the read-only spec directory.
        self._temp_dir: Optional[Path] = None
        self._lock_enabled = lock
        self._lock_handle = None

    def __enter__(self) -> Workspace:
        self._acquire_lock()
        try:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="acrf_", dir=self.target_path))
        except OSError:
            self._release_lock()
            raise
        logger.info("Workspace created at %s", self._temp_dir)
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> bool:
        self.cleanup()
        self._release_lock()
        return False

    # -- locking ------------------------------------------------------------
    def _acquire_lock(self) -> None:
        """Refuse to run two jobs into one target directory.

        Without this, concurrent runs interleave their publishes and the
        delivered PDF and workbook can come from different runs.
        """
        if not self._lock_enabled or fcntl is None:
            return
        path = self.target_path / self.LOCK_NAME
        # Deliberately not a context manager: the handle must stay open for the
        # lifetime of the run, because closing it releases the flock.
        handle = open(path, "w")  # noqa: SIM115
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise ConcurrentRunError(
                    f"Another aCRF run is already writing to {self.target_path}. "
                    "Wait for it to finish, or use a different output directory."
                ) from exc
            return  # locking unsupported here (e.g. some network mounts)
        handle.write(str(os.getpid()))
        handle.flush()
        self._lock_handle = handle

    def _release_lock(self) -> None:
        if self._lock_handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            self._lock_handle.close()
            # The lock file itself is left in place deliberately: unlinking it
            # lets a waiting process acquire a lock on an unlinked inode while
            # a third creates a fresh one, and both would think they hold it.
        except OSError:
            pass
        finally:
            self._lock_handle = None

    # -- paths --------------------------------------------------------------
    @property
    def temp_dir(self) -> Path:
        if self._temp_dir is None:
            raise RuntimeError("Workspace used outside of its context manager")
        return self._temp_dir

    @property
    def working_pdf(self) -> Path:
        """The PDF being built up (bookmarks, then annotations)."""
        return self.temp_dir / "acrf_working.pdf"

    @property
    def annotated_pdf(self) -> Path:
        return self.temp_dir / "acrf_annotated.pdf"

    @property
    def config_workbook(self) -> Path:
        return self.temp_dir / OUTPUT_CONFIG_NAME

    @property
    def out_pdf_path(self) -> Path:
        return self.target_path / OUTPUT_PDF_NAME

    @property
    def out_config_path(self) -> Path:
        return self.target_path / OUTPUT_CONFIG_NAME

    # -- publish / cleanup --------------------------------------------------
    def publish(self) -> OutputFiles:
        """Move the finished artefacts into the target directory, atomically.

        Each file is staged beside its destination and then renamed, so a
        consumer never observes a half-written aCRF — a plain copy that dies
        part-way leaves a truncated PDF that still looks like a deliverable.
        """
        outputs = OutputFiles()
        if self.annotated_pdf.exists():
            self._atomic_publish(self.annotated_pdf, self.out_pdf_path)
            outputs.pdf_path = self.out_pdf_path
            logger.info("Published %s", self.out_pdf_path)
        if self.config_workbook.exists():
            self._atomic_publish(self.config_workbook, self.out_config_path)
            outputs.config_path = self.out_config_path
            logger.info("Published %s", self.out_config_path)
        if not outputs:
            raise RuntimeError("Nothing to publish: no annotated PDF or config workbook produced")
        return outputs

    @staticmethod
    def _atomic_publish(source: Path, destination: Path) -> None:
        staged = destination.with_name(f".{destination.name}.staging")
        try:
            shutil.copy2(source, staged)
            os.replace(staged, destination)   # atomic within one filesystem
        finally:
            if staged.exists():
                staged.unlink(missing_ok=True)

    def cleanup(self) -> None:
        if self._temp_dir is None:
            return
        try:
            shutil.rmtree(self._temp_dir, ignore_errors=False)
            logger.info("Workspace cleaned up: %s", self._temp_dir)
        except OSError as exc:
            logger.warning("Could not remove workspace %s: %s", self._temp_dir, exc)
        finally:
            self._temp_dir = None
