"""Worker process: scheduler + Telegram bot. The only process that talks to the exchange.

Run: python -m app.worker
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Mode, get_settings, parse_hhmm
from app.db.session import get_sessionmaker, migrate
from app.services.engine import Clients, Engine
from app.services.state import alert, current_mode, get_state

log = logging.getLogger("edgecast.worker")


def setup_logging(level: str, data_dir: Any) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        logdir = data_dir / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(logdir / "worker.log", maxBytes=10_000_000, backupCount=5))
    except OSError:
        pass
    logging.basicConfig(level=level, handlers=handlers, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


class Worker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.sessions = get_sessionmaker()
        self.engine = Engine(self.sessions, self.settings, Clients.from_settings(self.settings))
        self.bot: Any = None
        self.stop = asyncio.Event()
        self.errors: dict[str, int] = {}

    def guarded(self, name: str, fn: Callable[[], Any]) -> Callable[[], None]:
        def run() -> None:
            try:
                fn()
                self.errors[name] = 0
            except Exception as e:
                n = self.errors.get(name, 0) + 1
                self.errors[name] = n
                log.exception("job %s failed", name)
                if n in (1, 5) or n % 20 == 0:
                    with self.sessions() as s:
                        alert(s, "health", f"❗ Worker job '{name}' failed ({n}x): {str(e)[:300]}")
                        s.commit()

        return run

    def tick(self) -> None:
        self.engine.check_kill_file()
        self.engine.process_commands()
        self.engine.heartbeat("worker", mode=self._mode().value)

    def _mode(self) -> Mode:
        with self.sessions() as s:
            return current_mode(s, self.settings)

    def live_upkeep(self) -> None:
        if self._mode() == Mode.LIVE:
            self.engine.reconcile_live()
            self.engine.sync_live_balance()

    def forecasts_then_strategy(self) -> None:
        self.engine.refresh_forecasts()
        self.engine.run_strategy()

    def bias_if_missing(self) -> None:
        with self.sessions() as s:
            last = get_state(s, "last_bias_fit")
        if not last:
            self.engine.refit_bias()

    def daily_summary(self) -> None:
        from app.alerts import daily_summary_text

        with self.sessions() as s:
            alert(s, "summary", daily_summary_text(s, self.settings))
            s.commit()

    def schedule(self, sched: AsyncIOScheduler) -> None:
        s = self.settings
        e = self.engine
        jobs: list[tuple[str, Callable[[], Any], dict[str, Any]]] = [
            ("tick", self.tick, {"seconds": 2}),
            ("markets", e.sync_markets, {"minutes": 5}),
            ("forecast+strategy", self.forecasts_then_strategy, {"minutes": 15}),
            ("strategy", e.run_strategy, {"minutes": 3}),
            ("orders", e.sync_orders, {"seconds": 20}),
            ("exits", e.run_exits, {"minutes": s.REEVAL_MINUTES}),
            ("resolutions", e.process_resolutions, {"minutes": 15}),
            ("equity", e.snapshot_equity, {"minutes": 5}),
            ("geoblock", e.check_geoblock, {"hours": 1}),
            ("reconcile", self.live_upkeep, {"minutes": 2}),
            ("bias", e.refit_bias, {"weeks": 1}),
        ]
        if s.MANUAL_WALLET_ADDRESS:
            jobs.append(("manual-import", e.import_manual_wallet, {"minutes": 10}))
        for name, fn, interval in jobs:
            sched.add_job(
                self.guarded(name, fn),
                "interval",
                id=name,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=60,
                **interval,
            )
        from app.services.backup import backup

        sched.add_job(
            self.guarded("backup", lambda: backup(s.DATABASE_URL, s.DATA_DIR, s.BACKUP_S3_URI)),
            CronTrigger(hour=3, minute=17, timezone=s.TZ),
            id="backup",
        )
        if s.TELEGRAM_ENABLED:
            t = parse_hhmm(s.TELEGRAM_DAILY_SUMMARY_TIME)
            sched.add_job(
                self.guarded("daily-summary", self.daily_summary),
                CronTrigger(hour=t.hour, minute=t.minute, timezone=s.TZ),
                id="daily-summary",
            )
        # Warm-up sequence right after start (staggered so the first strategy run has data).
        now = datetime.now(UTC)
        for name, fn, delay in (
            ("warm:geoblock", e.check_geoblock, 1),
            ("warm:markets", e.sync_markets, 2),
            ("warm:bias", self.bias_if_missing, 20),
            ("warm:forecast", self.forecasts_then_strategy, 40),
        ):
            sched.add_job(self.guarded(name, fn), "date", run_date=now + timedelta(seconds=delay), id=name)

    async def run(self) -> None:
        setup_logging(self.settings.LOG_LEVEL, self.settings.DATA_DIR)
        migrate()
        log.info(
            "worker starting (mode default %s, telegram %s)", self.settings.MODE.value, self.settings.TELEGRAM_ENABLED
        )
        await asyncio.to_thread(self.engine.startup)
        sched = AsyncIOScheduler(timezone=UTC)
        self.schedule(sched)
        sched.start()
        tasks = []
        if self.settings.TELEGRAM_ENABLED:
            from app.alerts import TelegramBot

            self.bot = TelegramBot(self.settings, self.sessions, self.engine)
            try:
                await self.bot.start()
                tasks.append(asyncio.create_task(self.bot.outbox_loop(self.stop)))
            except Exception:
                log.exception("telegram failed to start; continuing without it")
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)
        await self.stop.wait()
        log.info("worker stopping")
        sched.shutdown(wait=False)
        if self.bot is not None:
            await self.bot.stop()
        for t in tasks:
            t.cancel()


def main() -> None:
    asyncio.run(Worker().run())


if __name__ == "__main__":
    main()
