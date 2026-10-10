"""Shared helper for the wallet scripts: build an authenticated SDK client from .env (never prints the key)."""

from __future__ import annotations

import sys
from typing import Any


def secure_client() -> tuple[Any, Any]:
    try:
        from eth_account import Account
        from polymarket import SecureClient
    except ImportError:
        sys.exit("Install the live extra first: pip install -e '.[live]'")
    from app.config import get_settings

    s = get_settings()
    key = s.private_key()
    if not key:
        sys.exit("Set POLY_PRIVATE_KEY or POLY_PRIVATE_KEY_FILE in .env")
    signer = Account.from_key(key).address
    wallet = s.POLY_FUNDER_ADDRESS or signer
    client = SecureClient.create(private_key=key, wallet=wallet)
    print(f"signer {signer}\nwallet {client.wallet} ({client.wallet_type})")
    return client, s


def confirm(prompt: str, phrase: str = "yes") -> bool:
    return input(f"{prompt} Type '{phrase}' to continue: ").strip() == phrase
