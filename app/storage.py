from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.config import DownloadsConfig
from app.errors import DiskFullError, DiskLowError, JobSizeLimitError


@dataclass(frozen=True)
class DiskSummary:
    free_bytes: int
    min_free_bytes: int
    failed_bytes: int


def format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(max(value, 0))
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


class DownloadStorage:
    """Owns only program-created ``job-*`` directories under downloads/."""

    def __init__(self, config: DownloadsConfig) -> None:
        self.config = config

    def summary(self) -> DiskSummary:
        return DiskSummary(
            free_bytes=self.free_bytes(),
            min_free_bytes=self.config.min_free_bytes,
            failed_bytes=self.failed_bytes(),
        )

    def free_bytes(self) -> int:
        stats = os.statvfs(self.config.root)
        return int(stats.f_bavail * stats.f_frsize)

    def ensure_job_reservation(self, required_bytes: int) -> None:
        if required_bytes < 0:
            raise ValueError("required_bytes cannot be negative")
        if self.config.max_job_bytes and required_bytes > self.config.max_job_bytes:
            raise JobSizeLimitError(
                f"Job requires {required_bytes} bytes, above downloads.max_job_bytes "
                f"({self.config.max_job_bytes} bytes)"
            )

        free = self.free_bytes()
        if free - required_bytes < self.config.min_free_bytes:
            raise DiskLowError(
                f"Only {free} bytes are free; reserving {required_bytes} bytes would leave less "
                f"than downloads.min_free_bytes ({self.config.min_free_bytes} bytes)"
            )

    def ensure_progress_reservation(self, remaining_bytes: int) -> None:
        free = self.free_bytes()
        if free - max(remaining_bytes, 0) < self.config.min_free_bytes:
            raise DiskFullError(
                f"Available space fell below the configured reserve while downloading "
                f"(free={free} bytes, remaining={max(remaining_bytes, 0)} bytes)"
            )

    def cleanup_active_jobs(self) -> int:
        return self._remove_managed_jobs(self.config.active_dir)

    def failed_bytes(self) -> int:
        return sum(self._directory_size(path) for path in self._managed_job_dirs(self.config.failed_dir))

    def prune_failed_jobs(self) -> int:
        if not self.config.keep_failed or self.config.max_failed_bytes == 0:
            return self._remove_managed_jobs(self.config.failed_dir)

        removed = 0
        jobs = sorted(self._managed_job_dirs(self.config.failed_dir), key=lambda path: path.stat().st_mtime)
        total = sum(self._directory_size(path) for path in jobs)
        for path in jobs:
            if total <= self.config.max_failed_bytes:
                break
            total -= self._directory_size(path)
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        return removed

    def _managed_job_dirs(self, root: Path) -> list[Path]:
        if not root.exists():
            return []
        return [path for path in root.iterdir() if path.is_dir() and path.name.startswith("job-")]

    def _remove_managed_jobs(self, root: Path) -> int:
        removed = 0
        for path in self._managed_job_dirs(root):
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        return removed

    @staticmethod
    def _directory_size(path: Path) -> int:
        total = 0
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
        return total
