"""Public CLOB endpoints: order books, market fee info, price history. No auth."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.http import Http


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[Level] = field(default_factory=list)  # best (highest) first
    asks: list[Level] = field(default_factory=list)  # best (lowest) first
    tick_size: float | None = None
    min_order_size: float | None = None
    neg_risk: bool | None = None
    timestamp: str = ""

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def ask_depth(self, max_price: float) -> float:
        """Shares available at or below max_price."""
        return sum(lv.size for lv in self.asks if lv.price <= max_price + 1e-9)

    def bid_depth(self, min_price: float) -> float:
        return sum(lv.size for lv in self.bids if lv.price >= min_price - 1e-9)

    def ask_notional(self, max_price: float) -> float:
        return sum(lv.size * lv.price for lv in self.asks if lv.price <= max_price + 1e-9)

    def bid_notional(self, min_price: float) -> float:
        return sum(lv.size * lv.price for lv in self.bids if lv.price >= min_price - 1e-9)


def parse_book(data: dict[str, Any]) -> OrderBook:
    bids = sorted(
        (Level(float(x["price"]), float(x["size"])) for x in data.get("bids") or []), key=lambda lv: -lv.price
    )
    asks = sorted((Level(float(x["price"]), float(x["size"])) for x in data.get("asks") or []), key=lambda lv: lv.price)
    return OrderBook(
        token_id=str(data.get("asset_id") or data.get("token_id") or ""),
        bids=[b for b in bids if b.size > 0],
        asks=[a for a in asks if a.size > 0],
        tick_size=float(data["tick_size"]) if data.get("tick_size") else None,
        min_order_size=float(data["min_order_size"]) if data.get("min_order_size") else None,
        neg_risk=data.get("neg_risk"),
        timestamp=str(data.get("timestamp") or ""),
    )


@dataclass(frozen=True)
class FeeInfo:
    rate: float
    exponent: float

    def per_share(self, price: float) -> float:
        """Taker fee in USD per share at `price` (formula from the official SDK)."""
        if self.rate <= 0:
            return 0.0
        return float(self.rate * (price * (1.0 - price)) ** self.exponent)


@dataclass(frozen=True)
class ClobMarketInfo:
    condition_id: str
    tokens: tuple[str, ...]
    tick_size: float
    neg_risk: bool
    fee: FeeInfo | None  # None = the endpoint did not report fee data
    min_order_size: float | None


class Clob:
    def __init__(self, base_url: str, http: Http | None = None):
        self.http = http or Http("clob", base_url)

    def book(self, token_id: str) -> OrderBook:
        return parse_book(self.http.get("/book", token_id=token_id))

    def books(self, token_ids: list[str]) -> dict[str, OrderBook]:
        out: dict[str, OrderBook] = {}
        for i in range(0, len(token_ids), 50):
            chunk = token_ids[i : i + 50]
            data = self.http.post("/books", json=[{"token_id": t} for t in chunk])
            for raw in data or []:
                ob = parse_book(raw)
                out[ob.token_id] = ob
        return out

    def market_info(self, condition_id: str) -> ClobMarketInfo:
        d = self.http.get(f"/clob-markets/{condition_id}")
        tokens = tuple(str(t["t"]) for t in (d.get("t") or []) if t)
        fd = d.get("fd")
        fee = FeeInfo(float(fd.get("r", 0.0)), float(fd.get("e", 0.0))) if isinstance(fd, dict) else None
        return ClobMarketInfo(
            condition_id=condition_id,
            tokens=tokens,
            tick_size=float(d.get("mts") or 0.01),
            neg_risk=bool(d.get("nr", False)),
            fee=fee,
            min_order_size=float(d["mos"]) if d.get("mos") else None,
        )

    def prices_history(
        self,
        token_id: str,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
        interval: str | None = None,
        fidelity: int = 60,
    ) -> list[tuple[int, float]]:
        d = self.http.get(
            "/prices-history",
            market=token_id,
            startTs=start_ts,
            endTs=end_ts,
            interval=interval,
            fidelity=fidelity,
        )
        return [(int(p["t"]), float(p["p"])) for p in (d.get("history") or [])]
