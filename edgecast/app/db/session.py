"""Engine and session factory. SQLite runs in WAL mode so the web and worker can share one file."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def make_engine(url: str) -> Engine:
    kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        path = url.split("sqlite:///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn: Any, _: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return make_engine(get_settings().DATABASE_URL)


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def migrate(url: str | None = None) -> None:
    """Bring the schema to head with Alembic (safe to call on every start)."""
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parent
    cfg = Config()
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url or get_settings().DATABASE_URL)
    command.upgrade(cfg, "head")
