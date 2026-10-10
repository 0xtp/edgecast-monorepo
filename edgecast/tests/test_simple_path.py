"""The simple path: one-shot scan CLI and the read-only JSON API for an external (e.g. Vercel) front-end."""

from __future__ import annotations

import json

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app import cli
from app import config as config_mod
from tests.test_engine import make_engine

TOKEN = "t" * 32


def test_cli_scan_prints_signals(sessions, settings, capsys):
    eng, _ = make_engine(sessions, settings)
    assert cli.main(["scan"], engine=eng) == 0
    out = capsys.readouterr().out
    assert "BUY YES" in out and "74-75°F" in out and "polymarket.com/event/" in out
    assert cli.main(["scan", "--json", "--no-refresh"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows and rows[0]["action"].startswith("BUY") and 0 < rows[0]["edge_pct"] < 100
    assert cli.main(["status"]) == 0
    assert "Mode: SCAN" in capsys.readouterr().out


def test_cli_scan_empty(sessions, settings, capsys):
    assert cli.main(["scan", "--no-refresh"]) == 0
    assert "No active signals" in capsys.readouterr().out


@pytest.fixture
def api(env, monkeypatch, sessions):
    monkeypatch.setenv("API_TOKEN", TOKEN)
    monkeypatch.setenv("API_CORS_ORIGINS", "https://edgecast-view.vercel.app")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("UI_PASSWORD_HASH", bcrypt.hashpw(b"pw-pw-pw-pw", bcrypt.gensalt(4)).decode())
    config_mod.get_settings.cache_clear()
    from app.web.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_api_requires_token_and_is_read_only(api, sessions, settings):
    eng, _ = make_engine(sessions, config_mod.get_settings())
    eng.sync_markets()
    eng.refresh_forecasts()
    eng.run_strategy()
    assert api.get("/api/v1/signals").status_code == 401
    assert api.get("/api/v1/signals", headers={"Authorization": "Bearer nope"}).status_code == 401
    h = {"Authorization": f"Bearer {TOKEN}"}
    sig = api.get("/api/v1/signals", headers=h).json()
    assert sig and sig[0]["bucket"] == "74-75°F"
    st = api.get("/api/v1/status", headers=h).json()
    assert st["mode"] == "SCAN" and "books" in st
    assert api.get("/api/v1/positions", headers=h).json() == []
    assert api.get("/api/v1/equity?book=paper", headers=h).json() == []
    assert api.post("/api/v1/signals", headers=h).status_code == 405
    pre = api.options(
        "/api/v1/signals",
        headers={"Origin": "https://edgecast-view.vercel.app", "Access-Control-Request-Method": "GET"},
    )
    assert pre.headers.get("access-control-allow-origin") == "https://edgecast-view.vercel.app"
    bad = api.options(
        "/api/v1/signals", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
    )
    assert "access-control-allow-origin" not in bad.headers


def test_api_disabled_without_token(env, monkeypatch, sessions):
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    config_mod.get_settings.cache_clear()
    from app.web.main import create_app

    with TestClient(create_app()) as c:
        assert c.get("/api/v1/status", headers={"Authorization": "Bearer "}).status_code == 404


def test_short_api_token_rejected(env, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "short")
    with pytest.raises(Exception):
        config_mod.Settings()
