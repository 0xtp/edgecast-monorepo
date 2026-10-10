"""Exit rules for an open long position on one outcome token."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ExitKind(StrEnum):
    HOLD = "HOLD"
    TAKE_PROFIT = "TAKE_PROFIT"
    EDGE_REVERSAL = "EDGE_REVERSAL"


@dataclass(frozen=True)
class ExitDecision:
    kind: ExitKind
    reason: str


def decide_exit(
    *, p_model: float, best_bid: float | None, tp_buffer: float, exit_edge: float, new_forecast: bool
) -> ExitDecision:
    """Take profit when the market has caught up; bail when a new forecast run flips against us."""
    if best_bid is None:
        return ExitDecision(ExitKind.HOLD, "no bid")
    if new_forecast and p_model < best_bid - exit_edge:
        return ExitDecision(
            ExitKind.EDGE_REVERSAL,
            f"forecast flipped: p_model {100 * p_model:.1f}% < bid {100 * best_bid:.1f}% - {100 * exit_edge:.0f}pt",
        )
    if best_bid >= p_model - tp_buffer:
        return ExitDecision(
            ExitKind.TAKE_PROFIT,
            f"market caught up: bid {100 * best_bid:.1f}% >= p_model {100 * p_model:.1f}% - {100 * tp_buffer:.0f}pt",
        )
    return ExitDecision(ExitKind.HOLD, "hold to resolution")
