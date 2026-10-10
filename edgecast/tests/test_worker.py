from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.db.models import Heartbeat, Outbox


def test_worker_schedules_all_jobs_and_ticks(sessions, settings):
    from app.worker import Worker

    w = Worker()
    sched = AsyncIOScheduler()
    w.schedule(sched)
    ids = {j.id for j in sched.get_jobs()}
    for job in [
        "tick",
        "markets",
        "forecast+strategy",
        "strategy",
        "orders",
        "exits",
        "resolutions",
        "equity",
        "geoblock",
        "reconcile",
        "bias",
        "backup",
        "warm:geoblock",
        "warm:markets",
        "warm:forecast",
    ]:
        assert job in ids, job
    w.tick()
    with sessions() as s:
        assert s.get(Heartbeat, "worker") is not None


def test_guarded_job_alerts_on_failure(sessions, settings):
    from app.worker import Worker

    w = Worker()

    def boom():
        raise RuntimeError("kaput")

    w.guarded("demo", boom)()
    with sessions() as s:
        assert any("kaput" in o.text for o in s.scalars(select(Outbox)))


def test_backup(tmp_path, settings, sessions):
    from app.services.backup import backup

    out = backup(settings.DATABASE_URL, tmp_path)
    assert out is not None and out.exists() and out.stat().st_size > 0
    assert backup("postgresql://x/y", tmp_path) is None
