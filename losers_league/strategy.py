from typing import Any, Dict, List, Optional, Tuple

from .model import losing_probability


def rank_loser_candidates(games: List[Dict[str, Any]], used: Optional[set] = None) -> List[Dict[str, Any]]:
    """Compute losing probabilities for each team in each game and return ranked list of candidates.

    Each item: {
      'team': 'NYJ', 'opponent': 'BUF', 'home': bool,
      'spread_favorite_abbr': 'BUF', 'spread_points': 6.5,
      'lose_prob': 0.72, 'start': '...', 'source': 'ESPN: Caesars'
    }
    """
    used = used or set()
    candidates: List[Dict[str, Any]] = []
    for g in games:
        fav = g.get("spread_favorite_abbr")
        sp = g.get("spread_points")
        provider = g.get("odds_provider")
        start = g.get("start")
        home = g.get("home_abbr")
        away = g.get("away_abbr")

        for team, opp, is_home in [
            (home, away, True),
            (away, home, False),
        ]:
            p = losing_probability(team, favorite_abbr=fav, spread_points=sp)
            if p is None:
                # Fallback: 50/50 if no odds
                p = 0.5
            candidates.append({
                "team": team,
                "opponent": opp,
                "home": is_home,
                "spread_favorite_abbr": fav,
                "spread_points": sp,
                "lose_prob": p,
                "start": start,
                "source": f"ESPN: {provider}" if provider else "ESPN",
                "used": team in used,
            })

    candidates.sort(key=lambda x: x["lose_prob"], reverse=True)
    return candidates


def best_pick(candidates: List[Dict[str, Any]], used: Optional[set] = None) -> Optional[Dict[str, Any]]:
    used = used or set()
    for c in candidates:
        if c["team"] not in used:
            return c
    return None

