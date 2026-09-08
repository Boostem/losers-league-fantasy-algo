import datetime as dt
import json
import sys
from typing import Any, Dict, List, Optional

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover - fallback
    requests = None  # type: ignore
    import urllib.request  # type: ignore


# NOTE: this endpoint ignores a `year=` parameter entirely and always serves the
# newest season, which silently returned current-season games for any historical
# query. `dates=` is the parameter it actually honours.
SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?week={week}&dates={year}&seasontype=2"
)


def _http_get(url: str, timeout: int = 15) -> str:
    # ESPN's scoreboard host sits behind bot protection that rejects
    # unrecognised User-Agent strings with a 403, so send the client library's
    # own default rather than a custom one.
    if requests is not None:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text
    # Fallback to urllib
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # type: ignore
        return resp.read().decode("utf-8")


def fetch_week_games(year: int, week: int) -> List[Dict[str, Any]]:
    """Fetch games and odds for given NFL year/week from ESPN scoreboard API.

    Returns a list of dicts with keys:
    - id: str (event id)
    - start: datetime ISO string
    - home_abbr, away_abbr: str
    - home_name, away_name: str
    - odds_provider: Optional[str]
    - spread_favorite_abbr: Optional[str]
    - spread_points: Optional[float] (favorite minus underdog)
    """
    url = SCOREBOARD_URL.format(week=week, year=year)
    raw = _http_get(url)
    data = json.loads(raw)
    served = (data.get("season") or {}).get("year")
    if served is not None and int(served) != int(year):
        raise RuntimeError(
            f"ESPN served season {served} when asked for {year}; "
            "the 'dates' parameter is not being honoured."
        )
    events = data.get("events", [])
    games: List[Dict[str, Any]] = []

    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        if len(competitors) != 2:
            continue
        # ESPN lists competitors with homeAway flag
        home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])

        def _abbr(c: Dict[str, Any]) -> str:
            t = c.get("team") or {}
            return (t.get("abbreviation") or "").upper()

        def _name(c: Dict[str, Any]) -> str:
            t = c.get("team") or {}
            return t.get("displayName") or t.get("name") or _abbr(c)

        start = comp.get("date") or ev.get("date")
        odds_list = comp.get("odds") or ev.get("odds") or []
        provider = None
        spread_favorite_abbr: Optional[str] = None
        spread_points: Optional[float] = None

        # Pick the first odds entry with a spread if available
        for o in odds_list:
            details = (o or {}).get("details")
            prov = (o or {}).get("provider") or {}
            # "Live Odds" feeds carry the in-game number, not the line you bet
            # into, and would badly distort the pick.
            if "live odds" in str(prov.get("name") or "").lower():
                continue
            provider = prov.get("name") or provider
            # details looks like "KC -6.5"
            if isinstance(details, str) and details.strip():
                parts = details.split()
                if len(parts) >= 2:
                    fav = parts[0]
                    try:
                        sp = float(parts[1])
                        spread_favorite_abbr = fav.upper()
                        spread_points = abs(sp)
                        break
                    except Exception:
                        pass

            # Some feeds include explicit spread field
            sp_try = o.get("spread")
            fav_try = (o.get("favorite") or {}).get("abbreviation") or None
            if sp_try is not None and fav_try:
                try:
                    spread_points = abs(float(sp_try))
                    spread_favorite_abbr = str(fav_try).upper()
                    break
                except Exception:
                    pass

        games.append({
            "id": ev.get("id"),
            "start": start,
            "home_abbr": _abbr(home),
            "away_abbr": _abbr(away),
            "home_name": _name(home),
            "away_name": _name(away),
            "odds_provider": provider,
            "spread_favorite_abbr": spread_favorite_abbr,
            "spread_points": spread_points,
            # winner flag may be present post-game
            "home_winner": home.get("winner"),
            "away_winner": away.get("winner"),
            "status": (comp.get("status") or {}).get("type", {}).get("name"),
            "completed": (comp.get("status") or {}).get("type", {}).get("completed"),
        })

    return games


def team_result_from_games(games: List[Dict[str, Any]], team_abbr: str) -> Optional[str]:
    """Return 'win', 'loss', 'tie' or None (not found/not finished) for a team in provided games."""
    team = team_abbr.upper()
    for g in games:
        if g["home_abbr"] == team or g["away_abbr"] == team:
            comp = g
            completed = bool(comp.get("completed"))
            home_w = comp.get("home_winner")
            away_w = comp.get("away_winner")
            # ESPN may not set winner if not finished
            if not completed and (home_w is None and away_w is None):
                return None
            # If completed but winner flags missing, infer from score when available
            if completed and (home_w is None and away_w is None):
                try:
                    # ESPN often stores score as strings
                    hs = comp.get("competitors")[0 if comp["home_abbr"] == (comp.get("competitors")[0].get("team") or {}).get("abbreviation") else 1].get("score")
                except Exception:
                    hs = None
                # Fallback explicit mapping
                home_score = None
                away_score = None
                for c in (comp.get("competitors") or []):
                    try:
                        ab = (c.get("team") or {}).get("abbreviation")
                        sc = c.get("score")
                        scv = int(sc) if sc is not None else None
                        if ab == comp["home_abbr"]:
                            home_score = scv
                        elif ab == comp["away_abbr"]:
                            away_score = scv
                    except Exception:
                        pass
                if home_score is not None and away_score is not None:
                    if home_score > away_score:
                        home_w = True
                        away_w = False
                    elif away_score > home_score:
                        home_w = False
                        away_w = True
                    else:
                        return "tie"
            if home_w is True and comp["home_abbr"] == team:
                return "win"
            if away_w is True and comp["away_abbr"] == team:
                return "win"
            if home_w is False and comp["home_abbr"] == team:
                return "loss"
            if away_w is False and comp["away_abbr"] == team:
                return "loss"
            # Edge: tie
            return "tie"
    return None
