"""LIVE execution through the official Polymarket SDK (`polymarket-client`), behind a small adapter.

Idempotency: the intent row is committed before anything is sent (see base.place_order). Here we sign first,
store the signed order's salt, then post. If the post outcome is unknown (timeout, crash) the intent stays
PENDING and `reconcile` resolves it against the exchange. An intent is never re-sent.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import OrderIntent
from app.execution.fills import record_fill
from app.services.state import get_state, set_state

log = logging.getLogger(__name__)
LIVE_STATES = ("PENDING", "OPEN", "PARTIAL")
GOOD_TRADE_STATUSES = {"MATCHED", "MINED", "CONFIRMED", "RETRYING"}


@dataclass
class ExOrder:
    id: str
    token_id: str
    side: str
    price: float
    original_size: float
    size_matched: float
    created_at: datetime


@dataclass
class ExTrade:
    id: str
    token_id: str
    status: str
    matched_at: datetime
    # (order_id, price, size) for every leg that belongs to us
    legs: list[tuple[str, float, float]] = field(default_factory=list)
    side: str = ""


class TradingClient(Protocol):
    def sign_limit(self, token_id: str, price: float, size: float, side: str) -> tuple[Any, str]: ...
    def post(self, signed: Any) -> dict[str, Any]: ...
    def open_orders(self) -> list[ExOrder]: ...
    def trades(self, after: datetime | None) -> list[ExTrade]: ...
    def cancel(self, order_id: str) -> None: ...
    def cancel_all(self) -> None: ...
    def collateral_balance(self) -> float: ...
    def redeem(self, condition_id: str) -> str: ...


# ------------------------------------------------------------------ credential cache (encrypted)


def _fernet(secret: str) -> Any:
    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(hashlib.sha256(("edgecast-creds:" + secret).encode()).digest())
    return Fernet(key)


def save_creds(session: Session, secret: str, creds: dict[str, str]) -> None:
    token = _fernet(secret).encrypt(json.dumps(creds).encode()).decode()
    set_state(session, "clob_creds", token)


def load_creds(session: Session, secret: str) -> dict[str, str] | None:
    token = get_state(session, "clob_creds")
    if not token:
        return None
    try:
        return dict(json.loads(_fernet(secret).decrypt(token.encode())))
    except Exception:
        log.warning("cached CLOB credentials could not be decrypted; they will be re-derived")
        return None


# ------------------------------------------------------------------ SDK adapter


class SdkTradingClient:  # pragma: no cover - exercised only against the real exchange
    def __init__(self, settings: Settings, session: Session):
        try:
            from eth_account import Account
            from polymarket import ApiKeyCreds, SecureClient
        except ImportError as e:
            raise RuntimeError("LIVE needs the 'live' extra: pip install 'edgecast[live]'") from e
        key = settings.private_key()
        if not key:
            raise RuntimeError("LIVE needs POLY_PRIVATE_KEY or POLY_PRIVATE_KEY_FILE")
        signer = Account.from_key(key).address
        wallet = settings.POLY_FUNDER_ADDRESS or signer  # wallet == signer -> SDK classifies as EOA
        secret = settings.SESSION_SECRET.get_secret_value()
        cached = load_creds(session, secret)
        creds = (
            ApiKeyCreds(apiKey=cached["key"], secret=cached["secret"], passphrase=cached["passphrase"])
            if cached
            else None
        )
        self.client = SecureClient.create(private_key=key, wallet=wallet, credentials=creds)
        if not cached:
            c = self.client.credentials
            save_creds(session, secret, {"key": c.key, "secret": c.secret, "passphrase": c.passphrase})
        expected = settings.POLY_SIGNATURE_TYPE
        types = {"EOA": 0, "POLY_PROXY": 1, "GNOSIS_SAFE": 2}
        actual = types.get(str(self.client.wallet_type))
        if expected is not None and actual is not None and expected != actual:
            raise RuntimeError(
                f"POLY_SIGNATURE_TYPE={expected} but the SDK classified the wallet as {self.client.wallet_type}"
            )

    def sign_limit(self, token_id: str, price: float, size: float, side: str) -> tuple[Any, str]:
        signed = self.client.create_limit_order(
            token_id=token_id, price=Decimal(str(price)), size=Decimal(str(size)), side=side
        )
        return signed, str(signed.salt)

    def post(self, signed: Any) -> dict[str, Any]:
        resp = self.client.post_order(signed)
        if getattr(resp, "ok", False):
            return {"ok": True, "order_id": resp.order_id, "status": str(resp.status)}
        return {"ok": False, "error": f"{resp.code}: {resp.message}"}

    def open_orders(self) -> list[ExOrder]:
        return [
            ExOrder(
                o.id,
                str(o.asset_id),
                o.side,
                float(o.price),
                float(o.original_size),
                float(o.size_matched),
                o.created_at,
            )
            for o in self.client.list_open_orders()
        ]

    def trades(self, after: datetime | None) -> list[ExTrade]:
        out = []
        kw = {"after": str(int(after.timestamp()))} if after else {}
        for t in self.client.list_account_trades(**kw):
            legs = []
            if t.trader_side == "TAKER":
                legs.append((t.taker_order_id, float(t.price), float(t.size)))
            for mo in t.maker_orders:
                legs.append((mo.order_id, float(mo.price), float(mo.matched_amount)))
            out.append(ExTrade(t.id, str(t.asset_id), str(t.status), t.matched_at, legs, t.side))
        return out

    def cancel(self, order_id: str) -> None:
        self.client.cancel_order(order_id=order_id)

    def cancel_all(self) -> None:
        self.client.cancel_all()

    def collateral_balance(self) -> float:
        return self.client.get_balance_allowance(asset_type="COLLATERAL").balance / 1e6

    def redeem(self, condition_id: str) -> str:
        handle = self.client.redeem_positions(condition_id=condition_id)
        outcome = handle.wait()
        return str(getattr(outcome, "transaction_hash", outcome))


# ------------------------------------------------------------------ executor


class LiveExecutor:
    book = "LIVE"

    def __init__(self, client: TradingClient):
        self.client = client

    def submit(self, session: Session, intent: OrderIntent) -> None:
        try:
            signed, salt = self.client.sign_limit(intent.token_id, intent.price, intent.size, intent.side)
        except Exception as e:
            intent.status, intent.error = "FAILED", f"signing failed: {e}"
            return
        intent.signed_salt = salt
        session.commit()  # the signed order is on record before it leaves the process
        try:
            resp = self.client.post(signed)
        except Exception as e:  # unknown outcome -> stays PENDING for reconcile
            intent.error = f"post outcome unknown: {e}"
            return
        if resp.get("ok"):
            intent.exchange_order_id = resp["order_id"]
            intent.status = "OPEN"
        else:
            intent.status, intent.error = "REJECTED", str(resp.get("error"))[:500]

    def sync(self, session: Session) -> int:
        ours = {
            it.exchange_order_id: it
            for it in session.scalars(
                select(OrderIntent).where(
                    OrderIntent.mode == "LIVE",
                    OrderIntent.status.in_(LIVE_STATES),
                    OrderIntent.exchange_order_id.is_not(None),
                )
            )
        }
        if not ours:
            return 0
        oldest = min(it.created_at for it in ours.values()) - timedelta(minutes=5)
        n = 0
        for tr in self.client.trades(after=oldest):
            if tr.status.upper() not in GOOD_TRADE_STATUSES:
                continue
            for order_id, price, size in tr.legs:
                it = ours.get(order_id)
                if it is not None and record_fill(
                    session, it, trade_id=f"{tr.id}:{order_id}", price=price, size=size, ts=tr.matched_at
                ):
                    n += 1
        open_ids = {o.id for o in self.client.open_orders()}
        for oid, it in ours.items():
            if oid not in open_ids and it.status in ("OPEN", "PARTIAL"):
                it.status = "FILLED" if it.size - it.filled_size <= 1e-6 else "CANCELLED"
        return n

    def cancel(self, session: Session, intent: OrderIntent) -> None:
        if intent.exchange_order_id and intent.status in ("OPEN", "PARTIAL"):
            self.client.cancel(intent.exchange_order_id)
            intent.status = "CANCELLED"

    def cancel_all(self, session: Session) -> int:
        self.client.cancel_all()
        self.sync(session)
        rows = list(
            session.scalars(
                select(OrderIntent).where(OrderIntent.mode == "LIVE", OrderIntent.status.in_(("OPEN", "PARTIAL")))
            )
        )
        for it in rows:
            it.status = "CANCELLED"
        return len(rows)


def now() -> datetime:
    return datetime.now(UTC)
