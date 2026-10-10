"""Nightly SQLite backup (online backup API, consistent while the app runs) + optional S3 upload."""

from __future__ import annotations

import logging
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)
KEEP = 14


def sqlite_path(url: str) -> Path | None:
    if not url.startswith("sqlite:///"):
        return None
    return Path(url.split("sqlite:///", 1)[1])


def backup(database_url: str, data_dir: Path, s3_uri: str = "") -> Path | None:
    src = sqlite_path(database_url)
    if src is None or not src.exists():
        log.info("backup skipped (not SQLite or no file); use pg_dump for Postgres")
        return None
    out_dir = data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"edgecast-{datetime.now(UTC):%Y%m%d-%H%M%S}.db"
    with sqlite3.connect(src) as s, sqlite3.connect(dest) as d:
        s.backup(d)
    for old in sorted(out_dir.glob("edgecast-*.db"))[:-KEEP]:
        old.unlink(missing_ok=True)
    if s3_uri:
        try:
            subprocess.run(  # noqa: S603 - fixed argv, URI from operator env
                ["aws", "s3", "cp", str(dest), s3_uri.rstrip("/") + "/" + dest.name],  # noqa: S607
                check=True,
                capture_output=True,
                timeout=600,
            )
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("S3 upload failed (is the aws CLI installed and configured?): %s", e)
    log.info("backup written: %s", dest)
    return dest
