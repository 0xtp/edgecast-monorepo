"""Dashboard: auth, rate limiting, CSRF, every page renders, actions work."""

from __future__ import annotations

import re

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config as config_mod
from app.db.models import Command
from app.services.state import current_mode
from app.web import deps

PW = "correct horse battery"


@pytest.fixture
def client(env, monkeypatch, sessions):
    monkeypatch.setenv("UI_PASSWORD_HASH", bcrypt.hashpw(PW.encode(), bcrypt.gensalt(4)).decode())
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    config_mod.get_settings.cache_clear()
    deps._attempts.clear()
    from app.web.main import create_app

    with TestClient(create_app()) as c:
        yield c


def login(c: TestClient) -> str:
    r = c.post("/login", data={"username": "admin", "password": PW}, follow_redirects=False)
    assert r.status_code == 303
    page = c.get("/").text
    return re.search(r'"X-CSRF-Token": "([^"]+)"', page).group(1)


def test_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/signals?partial=1", headers={"HX-Request": "true"}).status_code == 401
    assert client.get("/login").status_code == 200


def test_bad_login_and_rate_limit(client):
    for _ in range(5):
        assert client.post("/login", data={"username": "admin", "password": "nope"}).status_code == 401
    r = client.post("/login", data={"username": "admin", "password": PW})
    assert r.status_code == 429  # locked out even with the right password


def test_pages_render(client, sessions):
    login(client)
    for path in [
        "/",
        "/signals",
        "/signals?partial=1&confidence=HIGH",
        "/positions",
        "/positions?tab=MANUAL",
        "/history",
        "/history?mode=PAPER",
        "/model",
        "/backtests",
        "/settings",
        "/audit",
    ]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "Traceback" not in r.text
    csv = client.get("/history.csv")
    assert csv.status_code == 200 and csv.text.startswith("created_at,mode")
    assert client.get("/backtests/../../etc/passwd").status_code == 404
    assert client.get("/backtests/missing.html").status_code == 404


def test_csrf_required(client):
    login(client)
    assert client.post("/action/pause").status_code == 403
    assert client.post("/action/pause", headers={"X-CSRF-Token": "wrong"}).status_code == 403


def test_actions(client, sessions, settings):
    tok = login(client)
    h = {"X-CSRF-Token": tok}
    r = client.post("/action/mode", data={"target": "PAPER"}, headers=h)
    assert r.status_code == 200 and "PAPER" in r.text and r.headers.get("HX-Refresh") == "true"
    with sessions() as s:
        assert current_mode(s).value == "PAPER"
    r = client.post("/action/mode", data={"target": "LIVE", "confirm_text": "CONFIRM LIVE"}, headers=h)
    assert "Cannot switch to LIVE" in r.text
    assert "Paused" in client.post("/action/pause", headers=h).text
    assert "Resumed" in client.post("/action/resume", headers=h).text
    assert "Kill switch" in client.post("/action/kill", headers=h).text
    assert (
        "ceiling" in client.post("/action/set", data={"key": "MAX_STAKE_PER_ORDER_USD", "value": "999"}, headers=h).text
    )
    assert "MIN_EDGE = 0.07" in client.post("/action/set", data={"key": "MIN_EDGE", "value": "0.07"}, headers=h).text
    assert "reset" in client.post("/action/reset", data={"key": "MIN_EDGE"}, headers=h).text
    assert "Close requested" in client.post("/action/close/1", headers=h).text
    assert "Logging manual trade" in client.post("/action/took/1", data={"price": "0.4", "usd": "10"}, headers=h).text
    assert "not found" in client.post("/action/ignore/1", headers=h).text
    with sessions() as s:
        kinds = [c.kind for c in s.scalars(select(Command))]
    assert "CANCEL_ALL" in kinds and "CLOSE_POSITION" in kinds and "TOOK" in kinds
    assert client.post("/logout", follow_redirects=False).status_code == 303


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["db"] == "ok" and r.json()["worker_ok"] is False


def test_session_secret_required(env, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "short")
    config_mod.get_settings.cache_clear()
    from app.web.main import create_app

    with pytest.raises(RuntimeError):
        create_app()


def test_filters():
    assert deps.fmt_usd(-3.5) == "-$3.50" and deps.fmt_usd(2, signed=True) == "+$2.00" and deps.fmt_usd(None) == "—"
    assert deps.fmt_pct(0.123) == "12.3%" and deps.fmt_pct(0.1, 0, True) == "+10%"
    assert deps.pnl_class(1) == "text-emerald-600" and deps.pnl_class(-1) == "text-red-600"
    assert deps.fmt_city("nyc") == "NYC" and deps.fmt_city("london") == "London"
    assert deps.fmt_ago(None) == "never" and deps.fmt_hours(None) == "—"
