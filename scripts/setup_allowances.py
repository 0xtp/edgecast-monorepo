"""One-time (re-runnable) trading approvals for the bot wallet.

Reads which approvals the current Polymarket contracts need (from the official SDK's environment config),
skips the ones already granted, asks for confirmation, then submits each approval and prints its
transaction hash. Safe to re-run: a second run finds nothing missing.

    python scripts/setup_allowances.py            # interactive
    python scripts/setup_allowances.py --yes      # no prompt
    python scripts/setup_allowances.py --include-perps   # also approve the perps deposit contract
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._wallet import confirm, secure_client


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--include-perps", action="store_true", help="also approve the perps deposit contract")
    a = ap.parse_args()
    client, _ = secure_client()
    from polymarket.environments import PRODUCTION

    perps = PRODUCTION._config.perps_deposit_contract.lower()
    state = client.get_trading_approvals_state()
    erc20 = [x for x in state.missing.erc20 if a.include_perps or x.spender.lower() != perps]
    erc1155 = list(state.missing.erc1155)
    if not erc20 and not erc1155:
        print("All required approvals are already in place. Nothing to do.")
        return
    print("Missing approvals:")
    for x in erc20:
        print(f"  ERC-20   token {x.token_address} -> spender {x.spender} (max)")
    for x in erc1155:
        print(f"  ERC-1155 token {x.token_address} -> operator {x.operator} (setApprovalForAll)")
    if client.wallet_type == "EOA":
        print("EOA wallet: each approval is an on-chain transaction paid in POL.")
    if not a.yes and not confirm("Submit these approvals?"):
        sys.exit("Aborted.")
    for x in erc20:
        h = client.approve_erc20(token_address=x.token_address, spender_address=x.spender, amount="max")
        print(f"  approve {x.spender}: tx {h.transaction_hash}")
        print(f"    -> {h.wait()}")
    for x in erc1155:
        h = client.approve_erc1155_for_all(token_address=x.token_address, operator_address=x.operator)
        print(f"  setApprovalForAll {x.operator}: tx {h.transaction_hash}")
        print(f"    -> {h.wait()}")
    print("Done. Re-run to verify: it should report nothing missing (indexed state can lag a few minutes).")


if __name__ == "__main__":
    main()
