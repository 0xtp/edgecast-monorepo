"""Container healthchecks: python -m app.tools.healthcheck worker|web"""

from __future__ import annotations

import sys
import urllib.request
from datetime import UTC, datetime


def worker(max_age_s: float = 120) -> int:
    from app.db.models import Heartbeat
    from app.db.session import session_scope

    with session_scope() as s:
        hb = s.get(Heartbeat, "worker")
        return 0 if hb and (datetime.now(UTC) - hb.ts).total_seconds() < max_age_s else 1


def web() -> int:
    with urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=5) as r:
        return 0 if r.status == 200 else 1


if __name__ == "__main__":
    sys.exit(worker() if (sys.argv[1:] or ["worker"])[0] == "worker" else web())
