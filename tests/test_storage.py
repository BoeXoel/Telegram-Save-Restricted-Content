from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.config import DownloadsConfig
from app.errors import DiskFullError, DiskLowError
from app.storage import DownloadStorage


class DownloadStorageTests(unittest.TestCase):
    def test_default_failed_cleanup_and_active_recovery_only_touch_job_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _downloads_config(Path(temp_dir), keep_failed=False, max_failed_bytes=0)
            _write_file(config.active_dir / "job-1" / "part.bin", 5)
            _write_file(config.active_dir / "manual" / "keep.txt", 5)
            _write_file(config.failed_dir / "job-2" / "part.bin", 5)
            _write_file(config.failed_dir / "manual" / "keep.txt", 5)
            storage = DownloadStorage(config)

            self.assertEqual(storage.cleanup_active_jobs(), 1)
            self.assertEqual(storage.prune_failed_jobs(), 1)

            self.assertFalse((config.active_dir / "job-1").exists())
            self.assertTrue((config.active_dir / "manual" / "keep.txt").exists())
            self.assertFalse((config.failed_dir / "job-2").exists())
            self.assertTrue((config.failed_dir / "manual" / "keep.txt").exists())

    def test_failed_cache_prunes_oldest_managed_job_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _downloads_config(Path(temp_dir), keep_failed=True, max_failed_bytes=10)
            old_job = config.failed_dir / "job-old"
            new_job = config.failed_dir / "job-new"
            _write_file(old_job / "part.bin", 10)
            _write_file(new_job / "part.bin", 10)
            os.utime(old_job, (1, 1))
            os.utime(new_job, (2, 2))
            storage = DownloadStorage(config)

            self.assertEqual(storage.prune_failed_jobs(), 1)

            self.assertFalse(old_job.exists())
            self.assertTrue(new_job.exists())
            self.assertEqual(storage.failed_bytes(), 10)

    def test_reservation_checks_preserve_the_minimum_free_space(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _downloads_config(Path(temp_dir), min_free_bytes=50)
            storage = DownloadStorage(config)
            storage.free_bytes = lambda: 100  # type: ignore[method-assign]

            with self.assertRaises(DiskLowError):
                storage.ensure_job_reservation(60)
            with self.assertRaises(DiskFullError):
                storage.ensure_progress_reservation(60)


def _downloads_config(
    root: Path,
    *,
    keep_failed: bool = False,
    max_failed_bytes: int = 0,
    min_free_bytes: int = 0,
) -> DownloadsConfig:
    active_dir = root / "active"
    failed_dir = root / "failed"
    completed_dir = root / "completed"
    for path in (root, active_dir, failed_dir, completed_dir):
        path.mkdir(parents=True, exist_ok=True)
    return DownloadsConfig(
        root=root,
        active_dir=active_dir,
        failed_dir=failed_dir,
        completed_dir=completed_dir,
        keep_failed=keep_failed,
        keep_completed=False,
        min_free_bytes=min_free_bytes,
        max_failed_bytes=max_failed_bytes,
        max_job_bytes=0,
    )


def _write_file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


if __name__ == "__main__":
    unittest.main()
