from typing import Optional


def favorite_win_prob_from_spread(spread_points: Optional[float]) -> Optional[float]:
    """Approximate the favorite's win probability from point spread.

    Uses a logistic approximation calibrated for NFL:
    p = 1 / (1 + exp(-k * spread)), with k ~= 0.148
    Clamped to [0.5, 0.98].
    """
    if spread_points is None:
        return None
    try:
        s = float(abs(spread_points))
    except Exception:
        return None
    # logistic mapping
    import math

    k = 0.148
    p = 1.0 / (1.0 + math.exp(-k * s))
    if p < 0.5:
        p = 0.5
    if p > 0.98:
        p = 0.98
    return p


def losing_probability(team_abbr: str, *, favorite_abbr: Optional[str], spread_points: Optional[float]) -> Optional[float]:
    """Return estimated probability that team loses the game given favorite and spread.

    If odds are missing, returns None.
    """
    if favorite_abbr is None or spread_points is None:
        return None
    fav_win = favorite_win_prob_from_spread(spread_points)
    if fav_win is None:
        return None
    if team_abbr.upper() == str(favorite_abbr).upper():
        return 1.0 - fav_win
    else:
        return fav_win

