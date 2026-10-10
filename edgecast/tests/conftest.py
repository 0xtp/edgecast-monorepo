from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app import config as config_mod
from app.db import session as session_mod


@pytest.fixture
def env(tmp_path, monkeypatch) -> Iterator[dict[str, str]]:
    values = {
        "DATABASE_URL": f"sqlite:///{tmp_path}/test.db",
        "DATA_DIR": str(tmp_path),
        "MODE": "SCAN",
        "TELEGRAM_ENABLED": "false",
        "SESSION_SECRET": "test-secret",
        "TZ": "UTC",
    }
    for k in list(os.environ):
        if k in config_mod.Settings.model_fields:
            monkeypatch.delenv(k, raising=False)
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)  # no stray .env
    config_mod.get_settings.cache_clear()
    session_mod.get_engine.cache_clear()
    session_mod.get_sessionmaker.cache_clear()
    yield values
    config_mod.get_settings.cache_clear()
    session_mod.get_engine.cache_clear()
    session_mod.get_sessionmaker.cache_clear()


@pytest.fixture
def settings(env):
    return config_mod.get_settings()


@pytest.fixture
def sessions(settings) -> sessionmaker[Session]:
    session_mod.migrate(settings.DATABASE_URL)
    return session_mod.get_sessionmaker()


@pytest.fixture
def db(sessions) -> Iterator[Session]:
    with sessions() as s:
        yield s
