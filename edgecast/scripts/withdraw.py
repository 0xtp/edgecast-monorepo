"""Sweep collateral from the bot wallet to your main address (WITHDRAW_TO_ADDRESS in .env).

    python scripts/withdraw.py --amount 50        # send 50 USD of collateral
    python scripts/withdraw.py --keep 200         # send everything above a 200 USD float
    python scripts/withdraw.py --all

Always asks you to type the destination address back. Never run this while the bot has open orders.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._wallet import secure_client


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--amount", type=float)
    g.add_argument("--keep", type=float)
    g.add_argument("--all", action="store_true")
    a = ap.parse_args()
    client, s = secure_client()
    to = s.WITHDRAW_TO_ADDRESS
    if not to:
        sys.exit("Set WITHDRAW_TO_ADDRESS in .env (your main wallet's public address).")
    from polymarket.environments import PRODUCTION

    token = PRODUCTION._config.collateral_token
    bal = client.get_balance_allowance(asset_type="COLLATERAL").balance  # base units (6 decimals)
    if a.all:
        amount = bal
    elif a.keep is not None:
        amount = max(0, bal - round(a.keep * 1e6))
    else:
        amount = round(a.amount * 1e6)
    if amount <= 0 or amount > bal:
        sys.exit(f"Nothing to send (balance {bal / 1e6:.2f}, requested {amount / 1e6:.2f}).")
    open_orders = list(client.list_open_orders())
    if open_orders:
        print(f"WARNING: {len(open_orders)} open orders. Kill/cancel them first or the bot may be under-funded.")
    print(f"Send {amount / 1e6:.6f} collateral ({token}) to {to}? Balance {bal / 1e6:.6f}.")
    if input("Type the destination address to confirm: ").strip().lower() != to.lower():
        sys.exit("Address mismatch. Aborted.")
    h = client.transfer_erc20(token_address=token, recipient_address=to, amount=amount)
    print(f"tx {h.transaction_hash}")
    print(h.wait())


if __name__ == "__main__":
    main()
