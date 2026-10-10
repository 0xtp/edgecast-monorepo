"""Generate UI_PASSWORD_HASH: python -m app.tools.hash_password"""

from __future__ import annotations

import getpass
import sys

import bcrypt


def main() -> None:
    pw = getpass.getpass("New UI password: ")
    if len(pw) < 12:
        sys.exit("Use at least 12 characters.")
    if getpass.getpass("Repeat: ") != pw:
        sys.exit("Passwords do not match.")
    h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt(12)).decode()
    print("\nAdd to .env (single quotes keep the $ signs literal in docker compose):")
    print(f"UI_PASSWORD_HASH='{h}'")


if __name__ == "__main__":
    main()
