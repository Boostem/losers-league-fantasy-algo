"""Spread -> win/lose probability.

The curve is fitted against real historical closing lines and outcomes by
``scripts/build_history.py``; the numbers it produces live in
``data/history.json``. The fallbacks below are what that fit returned, so the
module still behaves sensibly if the data file is missing.
"""

import json
import math
import os
from typing import Optional, Tuple

_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "history.json",
)

# Fitted on 2021-2025 closing lines. The CLI previously hardcoded k=0.148,
# which understates how often favourites actually win.
_FALLBACK_K = 0.155
_cached: Optional[Tuple[float, float]] = None


def model_params() -> Tuple[float, float]:
    """Return ``(intercept, slope)`` for p(home win) = sigmoid(a + b * home_line)."""
    global _cached
    if _cached is not None:
        return _cached
    intercept, slope = 0.0, _FALLBACK_K
    try:
        with open(_DATA_PATH, "r", encoding="utf-8") as fh:
            model = (json.load(fh) or {}).get("model") or {}
        intercept = float(model.get("intercept", 0.0))
        slope = float(model.get("slope", _FALLBACK_K))
    except (OSError, ValueError, TypeError):
        pass
    _cached = (intercept, slope)
    return _cached


def favorite_win_prob_from_spread(spread_points: Optional[float]) -> Optional[float]:
    """Approximate the favorite's win probability from a point spread."""
    if spread_points is None:
        return None
    try:
        s = float(abs(spread_points))
    except (TypeError, ValueError):
        return None
    _intercept, slope = model_params()
    # The fitted intercept is a small residual bias measured against the *home*
    # line, and this function only receives a favourite-relative spread with no
    # home/away context, so it is dropped. Costs under a point of probability;
    # index.html applies the full fit because it knows which side is home.
    p = 1.0 / (1.0 + math.exp(-slope * s))
    return min(0.98, max(0.5, p))


def losing_probability(team_abbr: str, *, favorite_abbr: Optional[str],
                       spread_points: Optional[float]) -> Optional[float]:
    """Estimated probability that ``team_abbr`` loses, or None without odds."""
    if favorite_abbr is None or spread_points is None:
        return None
    fav_win = favorite_win_prob_from_spread(spread_points)
    if fav_win is None:
        return None
    if team_abbr.upper() == str(favorite_abbr).upper():
        return 1.0 - fav_win
    return fav_win
