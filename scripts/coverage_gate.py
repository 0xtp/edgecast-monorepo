"""Fail CI unless the safety-critical modules are each covered above 85%."""

from __future__ import annotations

import sys

import coverage

TARGETS = ["app/markets/parser.py", "app/models/", "app/strategy/", "app/risk.py", "app/execution/"]
THRESHOLD = 85.0


def main() -> int:
    cov = coverage.Coverage()
    cov.load()
    data = cov.get_data()
    failed = False
    for target in TARGETS:
        files = [f for f in data.measured_files() if target in f.replace("\\", "/")]
        stmts = miss = 0
        for f in files:
            _, statements, _, missing, _ = cov.analysis2(f)
            stmts += len(statements)
            miss += len(missing)
        pct = 100.0 * (stmts - miss) / stmts if stmts else 0.0
        ok = pct >= THRESHOLD
        failed |= not ok
        print(f"{'OK  ' if ok else 'FAIL'} {target:28s} {pct:5.1f}% ({stmts - miss}/{stmts} lines)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
