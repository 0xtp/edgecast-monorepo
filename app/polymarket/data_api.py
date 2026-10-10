"""Data API: public positions and trades for an address (used for MANUAL_WALLET_ADDRESS import)."""

from __future__ import annotations

from typing import Any

from app.http import Http


class DataApi:
    def __init__(self, base_url: str, http: Http | None = None):
        self.http = http or Http("data_api", base_url)

    def positions(self, user: str, *, size_threshold: float = 0.01) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            batch = self.http.get("/positions", user=user, sizeThreshold=size_threshold, limit=500, offset=offset)
            out += list(batch or [])
            if not batch or len(batch) < 500:
                return out
            offset += 500

    def trades(self, user: str, limit: int = 500) -> list[dict[str, Any]]:
        return list(self.http.get("/trades", user=user, limit=limit) or [])
