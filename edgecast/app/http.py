"""Shared HTTP client with retries, timeouts and a last-call registry for the health panel."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

# name -> {"ok": datetime|None, "error": str, "error_at": datetime|None}
API_HEALTH: dict[str, dict[str, Any]] = {}

_RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    pass


class Http:
    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 20.0,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ):
        self.name = name
        self.retries = retries
        self.client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": "edgecast/0.1", **(headers or {})},
            transport=transport,
            follow_redirects=True,
        )

    def request(self, method: str, path: str, **kw: Any) -> Any:
        delay = 1.0
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.client.request(method, path, **kw)
                if resp.status_code in _RETRY_STATUS and attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                if resp.status_code >= 400:
                    raise HttpError(f"{self.name} {method} {path} -> {resp.status_code}: {resp.text[:200]}")
                self._ok()
                ctype = resp.headers.get("content-type", "")
                return resp.json() if "json" in ctype or resp.text[:1] in "[{" else resp.text
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last = e
                if attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
            except HttpError as e:
                self._fail(str(e))
                raise
        self._fail(str(last))
        raise HttpError(f"{self.name} {method} {path} failed: {last}") from last

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, json: Any) -> Any:
        return self.request("POST", path, json=json)

    def _ok(self) -> None:
        API_HEALTH.setdefault(self.name, {})["ok"] = datetime.now(UTC)

    def _fail(self, err: str) -> None:
        h = API_HEALTH.setdefault(self.name, {})
        h["error"], h["error_at"] = err[:300], datetime.now(UTC)
        log.warning("api error %s: %s", self.name, err[:300])

    def close(self) -> None:
        self.client.close()
