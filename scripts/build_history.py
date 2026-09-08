#!/usr/bin/env python3
"""Build the local data store for the Losers League tool.

Harvests completed NFL seasons from ESPN's public APIs, fits the
spread -> win-probability curve against what actually happened, derives
market-based team ratings, backtests pick strategies, and writes
``data/history.json`` + ``data/history.js`` for ``index.html`` to consume.

Usage:
    python3 scripts/build_history.py                 # default seasons
    python3 scripts/build_history.py --seasons 2021 2022 2023 2024 2025
    python3 scripts/build_history.py --refresh       # ignore cached responses

Only the standard library is required.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(REPO_ROOT, "data", "cache")
DATA_DIR = os.path.join(REPO_ROOT, "data")

SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
    "?week={week}&dates={season}&seasontype=2"
)
ODDS_URL = (
    "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
    "/events/{event}/competitions/{event}/odds"
)

# site.api.espn.com sits behind bot protection that 403s any User-Agent it does
# not recognise, including browser-looking strings sent by a non-browser. The
# stock urllib agent is accepted, so do not set one.
#
# Requests are still paced through a single global limiter so a wide worker
# pool cannot burst.
MIN_REQUEST_INTERVAL = 0.12
_throttle_lock = threading.Lock()
_last_request_at = [0.0]

# Regular season weeks. 2021 was the first 18-week season.
WEEKS = list(range(1, 19))
LAST_WEEK = WEEKS[-1]
DEFAULT_SEASONS = [2021, 2022, 2023, 2024, 2025]

# ESPN provider ids we will not treat as a closing line.
#   46 / 59   -> "Live Odds" feeds: in-game numbers, not the close.
#   1001-1003 -> projection models (accuscore, teamrankings, numberfire),
#                which are forecasts rather than market prices.
LIVE_ODDS_PROVIDER_IDS = {"46", "59"}
MODEL_PROVIDER_IDS = {"1001", "1002", "1003"}

# Preferred market sources, best first. Anything unlisted is still usable as a
# fallback provided it is not excluded above.
PROVIDER_PREFERENCE = ["58", "40", "31", "1004", "47", "48", "36", "41", "25", "55", "53"]

# How far ahead the lookahead strategy looks when pricing opportunity cost.
LOOKAHEAD_HORIZON = 6

# Offseason regression applied to the previous season's ratings before they
# seed the next season's prior.
CARRYOVER_SHRINK = 0.6

# 5-point Gauss-Hermite quadrature, used to average a probability over the
# uncertainty in a projected point spread.
_GH_NODES = [-2.020182870456086, -0.9585724646138185, 0.0,
             0.9585724646138185, 2.020182870456086]
_GH_WEIGHTS = [0.019953242059046, 0.393619323152241, 0.945308720482942,
               0.393619323152241, 0.019953242059046]
_GH_NORM = math.sqrt(math.pi)


# --------------------------------------------------------------------------
# HTTP with an on-disk cache
# --------------------------------------------------------------------------

def _pace() -> None:
    """Block until at least MIN_REQUEST_INTERVAL has passed since the last call."""
    while True:
        with _throttle_lock:
            wait = _last_request_at[0] + MIN_REQUEST_INTERVAL - time.monotonic()
            if wait <= 0:
                _last_request_at[0] = time.monotonic()
                return
        time.sleep(wait)


def http_get_json(url: str, cache_key: str, *, refresh: bool = False,
                  retries: int = 6) -> Optional[Any]:
    """GET a JSON document, memoised on disk under data/cache/.

    A 403 from ESPN means "slow down", not "never" — it is retried with a long
    backoff. A 404 means the document genuinely does not exist.
    """
    path = os.path.join(CACHE_DIR, cache_key + ".json")
    if not refresh and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            pass  # corrupt cache entry; refetch

    delay = 2.0
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            _pace()
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            return payload
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None  # genuinely absent (e.g. no odds recorded)
            last_err = exc
            if exc.code in (403, 429):
                delay = max(delay, 10.0)
        except Exception as exc:  # network hiccup, malformed body, ...
            last_err = exc
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
    print(f"  ! giving up on {url}: {last_err}", file=sys.stderr)
    return None


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_spread_details(details: Optional[str]) -> Optional[Tuple[Optional[str], float]]:
    """Parse an ESPN ``details`` string such as ``"KC -6.5"`` or ``"EVEN"``.

    Returns ``(favorite_abbr, points)`` with non-negative points, or
    ``(None, 0.0)`` for a pick'em; ``None`` when unparseable.
    """
    if not isinstance(details, str):
        return None
    text = details.strip()
    if not text:
        return None
    if text.upper() in {"EVEN", "PK", "PICK", "PICK'EM", "PICKEM"}:
        return (None, 0.0)
    parts = text.split()
    if len(parts) == 1:
        try:
            return (None, abs(float(parts[0])))
        except ValueError:
            return None
    fav = parts[0].upper()
    try:
        points = abs(float(parts[1]))
    except ValueError:
        return None
    if fav in {"EVEN", "PK"}:
        return (None, 0.0)
    return (fav, points)


def pick_closing_odds(odds_doc: Optional[Dict[str, Any]], home_abbr: str,
                      away_abbr: str) -> Dict[str, Any]:
    """Choose the best available closing spread from an ESPN odds document.

    ESPN mixes closing lines, live in-game lines and model projections into one
    list, so filter first and then take the most-preferred survivor.
    """
    blank: Dict[str, Any] = {"spread": None, "favorite": None, "provider": None}
    if not odds_doc:
        return blank

    usable: List[Tuple[int, Dict[str, Any]]] = []
    for item in odds_doc.get("items") or []:
        provider = item.get("provider") or {}
        pid = str(provider.get("id") or "")
        pname = str(provider.get("name") or "")
        if pid in LIVE_ODDS_PROVIDER_IDS or pid in MODEL_PROVIDER_IDS:
            continue
        if "live odds" in pname.lower():
            continue

        parsed = parse_spread_details(item.get("details"))
        favorite: Optional[str] = None
        points: Optional[float] = None
        if parsed is not None:
            favorite, points = parsed
        else:
            # Fall back to the numeric field, which ESPN signs from the home
            # team's perspective (negative => home favoured).
            raw = item.get("spread")
            if isinstance(raw, (int, float)):
                points = abs(float(raw))
                if raw < 0:
                    favorite = home_abbr
                elif raw > 0:
                    favorite = away_abbr
        if points is None:
            continue
        if points > 0 and favorite is None:
            continue  # a spread with no identifiable favourite is unusable
        if favorite is not None and favorite not in (home_abbr, away_abbr):
            continue

        try:
            rank = PROVIDER_PREFERENCE.index(pid)
        except ValueError:
            rank = len(PROVIDER_PREFERENCE)
        usable.append((rank, {"spread": points, "favorite": favorite,
                              "provider": pname or pid}))

    if not usable:
        return blank
    usable.sort(key=lambda pair: pair[0])
    return usable[0][1]


def parse_scoreboard(doc: Optional[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Extract games and bye teams from a scoreboard document."""
    if not doc:
        return [], []
    games: List[Dict[str, Any]] = []
    for event in doc.get("events") or []:
        comp = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        if len(competitors) != 2:
            continue
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue

        def abbr(c: Dict[str, Any]) -> str:
            return str((c.get("team") or {}).get("abbreviation") or "").upper()

        def score(c: Dict[str, Any]) -> Optional[int]:
            try:
                return int(c.get("score"))
            except (TypeError, ValueError):
                return None

        status = (comp.get("status") or {}).get("type") or {}
        games.append({
            "id": str(event.get("id") or ""),
            "date": comp.get("date") or event.get("date"),
            "home": abbr(home),
            "away": abbr(away),
            "homeScore": score(home),
            "awayScore": score(away),
            "completed": bool(status.get("completed")),
        })

    byes = [str(t.get("abbreviation") or "").upper()
            for t in ((doc.get("week") or {}).get("teamsOnBye") or [])]
    return games, [b for b in byes if b]


def home_line_of(game: Dict[str, Any]) -> Optional[float]:
    """Market-implied home margin: positive means the home team is favoured."""
    spread, fav = game.get("spread"), game.get("favorite")
    if spread is None:
        return None
    if fav == game["home"]:
        return float(spread)
    if fav == game["away"]:
        return -float(spread)
    return 0.0


# --------------------------------------------------------------------------
# Harvest
# --------------------------------------------------------------------------

def harvest_season(season: int, *, refresh: bool = False,
                   workers: int = 6) -> Dict[str, Any]:
    """Fetch every regular-season game for one season, with closing spreads."""
    print(f"[{season}] fetching schedule + results ...")
    weeks: Dict[int, Dict[str, Any]] = {}
    all_games: List[Dict[str, Any]] = []

    for week in WEEKS:
        doc = http_get_json(SCOREBOARD_URL.format(week=week, season=season),
                            f"scoreboard_{season}_{week}", refresh=refresh)
        # Guard against ESPN silently serving a different season: passing
        # `year=` instead of `dates=` does exactly that, and it is the bug the
        # original CLI shipped with.
        served = ((doc or {}).get("season") or {}).get("year")
        if served is not None and int(served) != season:
            raise RuntimeError(
                f"ESPN served season {served} when asked for {season}; "
                "the 'dates' parameter is not being honoured."
            )
        games, byes = parse_scoreboard(doc)
        weeks[week] = {"week": week, "byes": byes, "games": games}
        all_games.extend(games)

    print(f"[{season}] fetching closing odds for {len(all_games)} games ...")

    def fetch_odds(game: Dict[str, Any]) -> None:
        doc = http_get_json(ODDS_URL.format(event=game["id"]),
                            f"odds_{game['id']}", refresh=refresh)
        game.update(pick_closing_odds(doc, game["home"], game["away"]))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(fetch_odds, all_games))

    priced = sum(1 for g in all_games if g.get("spread") is not None)
    print(f"[{season}] {priced}/{len(all_games)} games have a closing spread")
    # A season that came back empty means the fetches failed, not that no
    # football was played. Without this the backtest would happily "survive"
    # a season it never actually simulated.
    if len(all_games) < 200:
        raise RuntimeError(
            f"only {len(all_games)} games harvested for {season} "
            "(expected ~272); the fetch failed — rerun to resume from cache"
        )
    if priced < 0.9 * len(all_games):
        print(f"  ! warning: only {priced} of {len(all_games)} games are priced",
              file=sys.stderr)
    return {"season": season, "weeks": [weeks[w] for w in WEEKS]}


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------

def sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def game_observations(seasons: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten harvested seasons into (line, outcome) observations."""
    obs: List[Dict[str, Any]] = []
    for season in seasons:
        for week in season["weeks"]:
            for game in week["games"]:
                if not game.get("completed"):
                    continue
                hs, as_ = game.get("homeScore"), game.get("awayScore")
                home_line = home_line_of(game)
                if hs is None or as_ is None or home_line is None:
                    continue
                obs.append({
                    "season": season["season"],
                    "week": week["week"],
                    "home": game["home"],
                    "away": game["away"],
                    "homeLine": home_line,
                    "spread": float(game["spread"]),
                    "favorite": game.get("favorite"),
                    "homeWin": 1.0 if hs > as_ else (0.5 if hs == as_ else 0.0),
                })
    return obs


def fit_logistic(points: Sequence[Tuple[float, float]], *, with_intercept: bool,
                 iterations: int = 200) -> Tuple[float, float]:
    """Fit ``p = sigmoid(a + b*x)`` by Newton-Raphson on the log-likelihood.

    ``points`` holds ``(x, y)`` pairs with y in [0, 1] (0.5 encodes a tie).
    ``a`` is pinned to 0 when ``with_intercept`` is False.
    """
    a, b = 0.0, 0.1
    for _ in range(iterations):
        g_a = g_b = h_aa = h_ab = h_bb = 0.0
        for x, y in points:
            p = sigmoid(a + b * x)
            w = p * (1.0 - p)
            resid = y - p
            g_a += resid
            g_b += resid * x
            h_aa += w
            h_ab += w * x
            h_bb += w * x * x
        h_aa += 1e-9
        h_bb += 1e-9
        if not with_intercept:
            if h_bb <= 0:
                break
            step = g_b / h_bb
            b += step
            if abs(step) < 1e-12:
                break
            continue
        det = h_aa * h_bb - h_ab * h_ab
        if abs(det) < 1e-12:
            break
        da = (g_a * h_bb - g_b * h_ab) / det
        db = (g_b * h_aa - g_a * h_ab) / det
        a += da
        b += db
        if abs(da) < 1e-12 and abs(db) < 1e-12:
            break
    return (a if with_intercept else 0.0), b


def brier(points: Sequence[Tuple[float, float]], a: float, b: float) -> float:
    if not points:
        return float("nan")
    return sum((sigmoid(a + b * x) - y) ** 2 for x, y in points) / len(points)


def log_loss(points: Sequence[Tuple[float, float]], a: float, b: float) -> float:
    if not points:
        return float("nan")
    total = 0.0
    for x, y in points:
        p = min(1 - 1e-9, max(1e-9, sigmoid(a + b * x)))
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(points)


def bucket_report(obs: Sequence[Dict[str, Any]], a: float, b: float,
                  k_legacy: float = 0.148) -> List[Dict[str, Any]]:
    """Empirical favourite win-rate by spread bucket vs the fitted model."""
    buckets = [(0.0, 0.5), (0.5, 2.5), (2.5, 3.5), (3.5, 6.5), (6.5, 7.5),
               (7.5, 10.5), (10.5, 14.5), (14.5, 99.0)]
    rows: List[Dict[str, Any]] = []
    for lo, hi in buckets:
        sel = [o for o in obs if lo <= o["spread"] < hi]
        if not sel:
            continue
        fav_wins = model = legacy = 0.0
        for o in sel:
            if o["favorite"] == o["home"]:
                fav_wins += o["homeWin"]
                model += sigmoid(a + b * o["homeLine"])
            elif o["favorite"] == o["away"]:
                fav_wins += 1.0 - o["homeWin"]
                model += 1.0 - sigmoid(a + b * o["homeLine"])
            else:
                fav_wins += 0.5
                model += 0.5
            legacy += min(0.98, max(0.5, sigmoid(k_legacy * o["spread"])))
        n = len(sel)
        rows.append({
            "bucket": f"{lo:g}-{hi:g}" if hi < 99 else f"{lo:g}+",
            "n": n,
            "empirical": round(fav_wins / n, 4),
            "model": round(model / n, 4),
            "legacy": round(legacy / n, 4),
        })
    return rows


# --------------------------------------------------------------------------
# Market-based team ratings
# --------------------------------------------------------------------------

def gaussian_solve(matrix: List[List[float]], rhs: List[float]) -> List[float]:
    """Solve a dense linear system with partial pivoting."""
    n = len(rhs)
    m = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            continue
        m[col], m[pivot] = m[pivot], m[col]
        pv = m[col][col]
        for r in range(n):
            if r == col:
                continue
            factor = m[r][col] / pv
            if factor == 0.0:
                continue
            for c in range(col, n + 1):
                m[r][c] -= factor * m[col][c]
    out = []
    for i in range(n):
        d = m[i][i]
        out.append(m[i][n] / d if abs(d) > 1e-12 else 0.0)
    return out


def solve_ridge(teams: Sequence[str], games: Sequence[Tuple[str, str, float]],
                *, prior: Dict[str, float], ridge: float,
                hfa: float) -> Dict[str, float]:
    """Least-squares team ratings from betting lines.

    Every game contributes ``rating[home] - rating[away] + hfa = home_line``.
    A ridge term pulls each rating toward ``prior`` so that early weeks — when
    barely any lines exist — stay sensible, and a weak zero-sum pull keeps the
    solution identified.
    """
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    ata = [[0.0] * n for _ in range(n)]
    aty = [0.0] * n

    for home, away, home_line in games:
        if home not in idx or away not in idx:
            continue
        h, a = idx[home], idx[away]
        target = home_line - hfa
        ata[h][h] += 1.0
        ata[a][a] += 1.0
        ata[h][a] -= 1.0
        ata[a][h] -= 1.0
        aty[h] += target
        aty[a] -= target

    for i, team in enumerate(teams):
        ata[i][i] += ridge
        aty[i] += ridge * prior.get(team, 0.0)
        for j in range(n):
            ata[i][j] += 1e-4

    return dict(zip(teams, gaussian_solve(ata, aty)))


def estimate_hfa(obs: Sequence[Dict[str, Any]]) -> float:
    """Average home-field points baked into the market's lines."""
    if not obs:
        return 1.5
    return sum(o["homeLine"] for o in obs) / len(obs)


def season_teams(season_doc: Dict[str, Any]) -> List[str]:
    return sorted({t for w in season_doc["weeks"] for g in w["games"]
                   for t in (g["home"], g["away"]) if t})


def season_ratings(season_doc: Dict[str, Any], *, through_week: Optional[int],
                   hfa: float, prior: Optional[Dict[str, float]] = None,
                   ridge: float = 4.0) -> Dict[str, float]:
    """Ratings from lines posted before ``through_week`` (None = whole season)."""
    games: List[Tuple[str, str, float]] = []
    for week in season_doc["weeks"]:
        if through_week is not None and week["week"] >= through_week:
            break
        for game in week["games"]:
            line = home_line_of(game)
            if line is not None:
                games.append((game["home"], game["away"], line))
    return solve_ridge(season_teams(season_doc), games, prior=prior or {},
                       ridge=ridge, hfa=hfa)


def carryover(ratings: Dict[str, float], shrink: float = CARRYOVER_SHRINK) -> Dict[str, float]:
    """Regress a season's final ratings toward average for use as a prior."""
    return {t: v * shrink for t, v in ratings.items()}


# --------------------------------------------------------------------------
# Season model: ratings, real lines and projections, precomputed per week
# --------------------------------------------------------------------------

def lose_prob_uncertain(home_line: float, *, is_home: bool, a: float, b: float,
                        sigma: float) -> float:
    """Probability a team loses, averaging over uncertainty in a projected line.

    A projected spread is not a posted spread: the real number, once books put
    it up, will land somewhere around the projection. Integrating the win
    curve over that spread of outcomes matters because the *best* future
    opportunity is a maximum over noisy quantities, and ignoring the noise
    systematically understates it.
    """
    if sigma <= 1e-9:
        p_home_win = sigmoid(a + b * home_line)
    else:
        acc = 0.0
        for node, weight in zip(_GH_NODES, _GH_WEIGHTS):
            line = home_line + sigma * math.sqrt(2.0) * node
            acc += weight * sigmoid(a + b * line)
        p_home_win = acc / _GH_NORM
    return 1.0 - (p_home_win if is_home else 1.0 - p_home_win)


class SeasonModel:
    """Everything the pick strategies need for one season, precomputed.

    Ratings and projections depend only on the season and the decision week,
    never on simulated outcomes, so they are computed once and shared across
    every Monte-Carlo trial.
    """

    def __init__(self, season_doc: Dict[str, Any], *, hfa: float, a: float,
                 b: float, proj_intercept: float, proj_scale: float,
                 proj_sigma: float, prior: Optional[Dict[str, float]] = None):
        self.season = season_doc["season"]
        self.doc = season_doc
        self.hfa = hfa
        self.a = a
        self.b = b
        self.proj_intercept = proj_intercept
        self.proj_scale = proj_scale
        self.proj_sigma = proj_sigma

        self.ratings: Dict[int, Dict[str, float]] = {
            w: season_ratings(season_doc, through_week=w, hfa=hfa, prior=prior)
            for w in WEEKS
        }
        self.final_ratings = season_ratings(season_doc, through_week=None,
                                            hfa=hfa, prior=prior)
        self.actual: Dict[int, List[Dict[str, Any]]] = {
            w: self._week_actual(w) for w in WEEKS
        }
        self.results: Dict[int, Dict[str, str]] = {
            w: self._week_results(w) for w in WEEKS
        }
        self.future_best: Dict[int, Dict[str, float]] = {
            w: self._future_best(w) for w in WEEKS
        }

    def _games(self, week_no: int) -> List[Dict[str, Any]]:
        week = next((w for w in self.doc["weeks"] if w["week"] == week_no), None)
        return week["games"] if week else []

    def _week_actual(self, week_no: int) -> List[Dict[str, Any]]:
        """Candidates for a week priced off the real closing line."""
        out: List[Dict[str, Any]] = []
        for game in self._games(week_no):
            line = home_line_of(game)
            if line is None:
                continue
            p_home_win = sigmoid(self.a + self.b * line)
            for team, opp, is_home in ((game["home"], game["away"], True),
                                       (game["away"], game["home"], False)):
                p_win = p_home_win if is_home else 1.0 - p_home_win
                out.append({"team": team, "opponent": opp, "home": is_home,
                            "loseProb": 1.0 - p_win})
        out.sort(key=lambda c: c["loseProb"], reverse=True)
        return out

    def _week_results(self, week_no: int) -> Dict[str, str]:
        results: Dict[str, str] = {}
        for game in self._games(week_no):
            hs, as_ = game.get("homeScore"), game.get("awayScore")
            if not game.get("completed") or hs is None or as_ is None:
                continue
            if hs == as_:
                results[game["home"]] = results[game["away"]] = "tie"
            else:
                winner = game["home"] if hs > as_ else game["away"]
                loser = game["away"] if hs > as_ else game["home"]
                results[winner] = "win"
                results[loser] = "loss"
        return results

    def projected_lose_probs(self, week_no: int, ratings: Dict[str, float]) -> Dict[str, float]:
        """Lose probabilities for a week priced only off ratings, not lines."""
        out: Dict[str, float] = {}
        for game in self._games(week_no):
            raw = (ratings.get(game["home"], 0.0) - ratings.get(game["away"], 0.0)
                   + self.hfa)
            line = self.proj_intercept + self.proj_scale * raw
            for team, is_home in ((game["home"], True), (game["away"], False)):
                out[team] = lose_prob_uncertain(line, is_home=is_home, a=self.a,
                                                b=self.b, sigma=self.proj_sigma)
        return out

    def _future_best(self, week_no: int) -> Dict[str, float]:
        """Each team's best projected chance to lose in the coming weeks.

        Judged from what is knowable at ``week_no``: ratings built only from
        earlier weeks, and projections rather than lines that are not posted
        yet. That keeps the backtest honest — no peeking at closing numbers
        the tool would not have had on the day.
        """
        ratings = self.ratings[week_no]
        best: Dict[str, float] = {}
        for future in range(week_no + 1, min(week_no + LOOKAHEAD_HORIZON, LAST_WEEK) + 1):
            for team, prob in self.projected_lose_probs(future, ratings).items():
                if prob > best.get(team, 0.0):
                    best[team] = prob
        return best


def choose(model: SeasonModel, week_no: int, used: set, *,
           strategy: str) -> Optional[Dict[str, Any]]:
    """Pick a team for ``week_no`` under the given strategy."""
    cands = [c for c in model.actual[week_no] if c["team"] not in used]
    if not cands:
        return None
    if strategy == "greedy":
        return cands[0]
    if strategy == "assignment":
        return choose_assignment(model, week_no, used)
    if strategy != "lookahead":
        raise ValueError(f"unknown strategy {strategy}")

    future_best = model.future_best[week_no]
    # When teams are plentiful relative to the weeks left there is room to
    # hoard a good underdog; late in the season there is not.
    weeks_left = LAST_WEEK - week_no
    scarcity = min(1.0, weeks_left / 12.0)

    best, best_score = None, float("-inf")
    for cand in cands:
        later = future_best.get(cand["team"], 0.0)
        cost = max(0.0, later - cand["loseProb"]) * 0.5 * scarcity
        score = cand["loseProb"] - cost
        if score > best_score:
            best, best_score = cand, score
    return best or cands[0]


def simulate_actual(model: SeasonModel, *, strategy: str, saves: int = 1,
                    tie_survives: bool = True) -> Dict[str, Any]:
    """Replay a season against what really happened."""
    used: set = set()
    saves_left = saves
    picks: List[Dict[str, Any]] = []

    for week_no in WEEKS:
        pick = choose(model, week_no, used, strategy=strategy)
        if pick is None:
            break
        result = model.results[week_no].get(pick["team"])
        used.add(pick["team"])
        picks.append({"week": week_no, "team": pick["team"],
                      "loseProb": round(pick["loseProb"], 4), "result": result})
        survived = result == "loss" or (result == "tie" and tie_survives)
        if not survived:
            if saves_left > 0:
                saves_left -= 1
            else:
                return {"strategy": strategy, "season": model.season,
                        "survivedWeeks": week_no - 1, "survivedSeason": False,
                        "outWeek": week_no, "picks": picks}
    return {"strategy": strategy, "season": model.season,
            "survivedWeeks": len(picks), "survivedSeason": True,
            "outWeek": None, "picks": picks}


def strategy_sequence(model: SeasonModel, strategy: str) -> List[float]:
    """The lose probabilities a strategy would line up for a whole season.

    None of these strategies consult results — only the week and which teams
    are spent — so the sequence of picks is fixed in advance. That is what
    makes the exact evaluation below possible.
    """
    used: set = set()
    probs: List[float] = []
    for week_no in WEEKS:
        pick = choose(model, week_no, used, strategy=strategy)
        if pick is None:
            break
        used.add(pick["team"])
        probs.append(pick["loseProb"])
    return probs


def exact_survival(lose_probs: Sequence[float], saves: int) -> float:
    """P(surviving the season) = P(at most `saves` picks fail).

    Because the pick sequence is deterministic, this Poisson-binomial DP gives
    the answer outright — no sampling error to squint through.
    """
    dp = [1.0] + [0.0] * saves
    for p in lose_probs:
        nxt = [0.0] * (saves + 1)
        for failures, mass in enumerate(dp):
            if mass == 0.0:
                continue
            nxt[failures] += mass * p
            if failures + 1 <= saves:
                nxt[failures + 1] += mass * (1.0 - p)
        dp = nxt
    return sum(dp)


def expected_weeks(lose_probs: Sequence[float], saves: int) -> float:
    """Expected number of weeks completed before elimination."""
    dp = [1.0] + [0.0] * saves
    alive = 1.0
    total = 0.0
    for p in lose_probs:
        nxt = [0.0] * (saves + 1)
        dead = 0.0
        for failures, mass in enumerate(dp):
            if mass == 0.0:
                continue
            nxt[failures] += mass * p
            if failures + 1 <= saves:
                nxt[failures + 1] += mass * (1.0 - p)
            else:
                dead += mass * (1.0 - p)
        dp = nxt
        alive -= dead
        total += alive
    return total


def hungarian_max(matrix: List[List[float]], n_rows: int,
                  n_cols: int) -> Dict[int, int]:
    """Max-weight bipartite assignment (rows <= cols), by the O(n^3) method."""
    inf = float("inf")
    cost = [[-matrix[r][c] for c in range(n_cols)] for r in range(n_rows)]
    u = [0.0] * (n_rows + 1)
    v = [0.0] * (n_cols + 1)
    p = [0] * (n_cols + 1)
    way = [0] * (n_cols + 1)
    for i in range(1, n_rows + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (n_cols + 1)
        used = [False] * (n_cols + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = 0
            for j in range(1, n_cols + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(n_cols + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    return {p[j] - 1: j - 1 for j in range(1, n_cols + 1) if p[j]}


def choose_assignment(model: SeasonModel, week_no: int,
                      used: set) -> Optional[Dict[str, Any]]:
    """Plan distinct teams across every remaining week, then play this week's.

    Maximising the summed log lose-probability is the quantity survival
    actually depends on, so this is the textbook-optimal plan *given* the
    inputs. It is included to show that the inputs are the problem: the future
    weeks it plans around are projections, not posted lines.
    """
    cands = [c for c in model.actual[week_no] if c["team"] not in used]
    if not cands:
        return None
    weeks = list(range(week_no, LAST_WEEK + 1))
    teams = sorted({c["team"] for w in weeks for c in model.actual[w]
                    if c["team"] not in used})
    if len(teams) < len(weeks):
        return cands[0]
    tidx = {t: i for i, t in enumerate(teams)}
    ratings = model.ratings[week_no]

    unplayable = -25.0
    mat = [[unplayable] * len(teams) for _ in weeks]
    for row, w in enumerate(weeks):
        if w == week_no:
            for c in model.actual[w]:
                if c["team"] in tidx:
                    mat[row][tidx[c["team"]]] = math.log(max(1e-6, c["loseProb"]))
        else:
            for team, prob in model.projected_lose_probs(w, ratings).items():
                if team in tidx:
                    mat[row][tidx[team]] = math.log(max(1e-6, prob))

    col = hungarian_max(mat, len(weeks), len(teams)).get(0)
    if col is None:
        return cands[0]
    want = teams[col]
    return next((c for c in cands if c["team"] == want), cands[0])


# --------------------------------------------------------------------------
# Projection quality: how well do ratings stand in for a real line?
# --------------------------------------------------------------------------

def fit_projection(seasons: Sequence[Dict[str, Any]], *, hfa: float,
                   priors: Dict[int, Dict[str, float]],
                   min_week: int = 2) -> Tuple[float, float, float, int]:
    """Regress the real closing line on the rating-implied line.

    Returns ``(intercept, scale, residual_sigma, n)``. Ratings are shrunk by a
    ridge penalty, so the rating-implied spread is systematically flatter than
    the number books actually post; this measures by how much, and how much
    scatter is left over.
    """
    xs: List[float] = []
    ys: List[float] = []
    for season_doc in seasons:
        prior = priors.get(season_doc["season"], {})
        for week_no in WEEKS:
            if week_no < min_week:
                continue
            ratings = season_ratings(season_doc, through_week=week_no, hfa=hfa,
                                     prior=prior)
            week = next(w for w in season_doc["weeks"] if w["week"] == week_no)
            for game in week["games"]:
                actual = home_line_of(game)
                if actual is None:
                    continue
                xs.append(ratings.get(game["home"], 0.0)
                          - ratings.get(game["away"], 0.0) + hfa)
                ys.append(actual)

    n = len(xs)
    if n < 50:
        return 0.0, 1.0, 6.0, n
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    scale = sxy / sxx if sxx > 1e-9 else 1.0
    intercept = mean_y - scale * mean_x
    resid = sum((y - (intercept + scale * x)) ** 2 for x, y in zip(xs, ys))
    sigma = math.sqrt(resid / max(1, n - 2))
    return intercept, scale, sigma, n


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seasons", type=int, nargs="+", default=DEFAULT_SEASONS)
    parser.add_argument("--refresh", action="store_true",
                        help="Ignore cached responses and refetch everything")
    parser.add_argument("--workers", type=int, default=3,
                        help="Concurrent odds requests (all are rate-limited)")
    args = parser.parse_args(argv)

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    seasons = [harvest_season(s, refresh=args.refresh, workers=args.workers)
               for s in sorted(args.seasons)]

    obs = game_observations(seasons)
    print(f"\nCalibrating on {len(obs)} completed games with a closing spread.")
    if len(obs) < 200:
        print("! Too few observations to calibrate; aborting.", file=sys.stderr)
        return 1

    points = [(o["homeLine"], o["homeWin"]) for o in obs]
    a_fit, b_fit = fit_logistic(points, with_intercept=True)
    _, b_only = fit_logistic(points, with_intercept=False)
    hfa = estimate_hfa(obs)

    print("\n─── Spread → win probability ───")
    print(f"  fitted:  p(home win) = sigmoid({a_fit:+.4f} + {b_fit:.5f} * home_line)")
    print(f"  slope with no intercept, k = {b_only:.5f}   (CLI hardcoded 0.148)")
    print(f"  mean home line (market home-field edge) = {hfa:+.2f} pts")
    print(f"  Brier   fitted={brier(points, a_fit, b_fit):.4f}  "
          f"legacy(k=0.148)={brier(points, 0.0, 0.148):.4f}")
    print(f"  LogLoss fitted={log_loss(points, a_fit, b_fit):.4f}  "
          f"legacy(k=0.148)={log_loss(points, 0.0, 0.148):.4f}")

    rows = bucket_report(obs, a_fit, b_fit)
    print("\n─── Favourite win rate by spread (empirical vs model) ───")
    print(f"  {'spread':>9} {'n':>5} {'actual':>8} {'fitted':>8} {'k=.148':>8}")
    for row in rows:
        print(f"  {row['bucket']:>9} {row['n']:>5} {row['empirical']:>8.3f} "
              f"{row['model']:>8.3f} {row['legacy']:>8.3f}")

    # Priors: each season is seeded by the previous season's final ratings.
    priors: Dict[int, Dict[str, float]] = {}
    running: Dict[str, float] = {}
    for season_doc in seasons:
        priors[season_doc["season"]] = dict(running)
        running = carryover(season_ratings(season_doc, through_week=None,
                                           hfa=hfa, prior=priors[season_doc["season"]]))

    proj_intercept, proj_scale, proj_sigma, proj_n = fit_projection(
        seasons, hfa=hfa, priors=priors)
    print("\n─── Rating-implied line vs real closing line ───")
    print(f"  actual ≈ {proj_intercept:+.2f} + {proj_scale:.2f} × projected   "
          f"(n={proj_n}, residual σ={proj_sigma:.2f} pts)")

    models = [
        SeasonModel(doc, hfa=hfa, a=a_fit, b=b_fit, proj_intercept=proj_intercept,
                    proj_scale=proj_scale, proj_sigma=proj_sigma,
                    prior=priors[doc["season"]])
        for doc in seasons
    ]

    strategies = ("greedy", "lookahead", "assignment")

    print("\n─── Backtest against real outcomes (1 save, tie survives) ───")
    backtests: List[Dict[str, Any]] = []
    for model in models:
        line = f"  {model.season}  "
        for strategy in strategies:
            res = simulate_actual(model, strategy=strategy)
            backtests.append(res)
            status = "survived" if res["survivedSeason"] else f"out wk {res['outWeek']}"
            line += f"{strategy}: {status:<12} "
        print(line)

    summary: Dict[str, Any] = {}
    for strategy in strategies:
        runs = [r for r in backtests if r["strategy"] == strategy]
        summary[strategy] = {
            "seasonsSurvived": sum(1 for r in runs if r["survivedSeason"]),
            "seasons": len(runs),
            "meanWeeks": round(sum(r["survivedWeeks"] for r in runs) / len(runs), 2),
        }
        print(f"  {strategy:<11} survived {summary[strategy]['seasonsSurvived']}"
              f"/{summary[strategy]['seasons']} seasons, "
              f"mean {summary[strategy]['meanWeeks']:.1f} weeks")

    # Five seasons is a thin sample, and the real outcome of any one of them is
    # mostly luck. Since each strategy's pick sequence is fixed in advance, the
    # survival probability of that sequence can be computed outright, which
    # separates the strategies far more sharply than five coin flips can.
    print("\n─── Exact survival probability of each strategy's pick sequence ───")
    sequences = {s: {m.season: strategy_sequence(m, s) for m in models}
                 for s in strategies}
    exact: Dict[str, Any] = {}
    for saves in (0, 1, 2):
        print(f"  with {saves} save{'' if saves == 1 else 's'}:")
        for strategy in strategies:
            per_season = [exact_survival(sequences[strategy][m.season], saves)
                          for m in models]
            per_weeks = [expected_weeks(sequences[strategy][m.season], saves)
                         for m in models]
            rate = sum(per_season) / len(per_season)
            wks = sum(per_weeks) / len(per_weeks)
            exact.setdefault(strategy, {})[str(saves)] = {
                "survival": round(rate, 5), "expectedWeeks": round(wks, 3),
            }
            print(f"    {strategy:<11} survival {rate:7.2%}   "
                  f"expected weeks {wks:5.2f}")
    print("  Planning around projected future lines does not pay: a posted line"
          "\n  beats a projection, and trading one for the other loses ground.")

    latest = models[-1]
    ranked = sorted(latest.final_ratings.items(), key=lambda kv: kv[1], reverse=True)
    print(f"\n─── Market ratings, end of {latest.season} (points vs average) ───")
    print("  best:  " + ", ".join(f"{t} {v:+.1f}" for t, v in ranked[:5]))
    print("  worst: " + ", ".join(f"{t} {v:+.1f}" for t, v in ranked[-5:]))

    payload = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seasons": [m.season for m in models],
        "model": {
            "intercept": round(a_fit, 6),
            "slope": round(b_fit, 6),
            "slopeNoIntercept": round(b_only, 6),
            "legacyK": 0.148,
            "homeFieldPoints": round(hfa, 4),
            "sampleSize": len(obs),
            "brier": round(brier(points, a_fit, b_fit), 6),
            "brierLegacy": round(brier(points, 0.0, 0.148), 6),
            "logLoss": round(log_loss(points, a_fit, b_fit), 6),
            "logLossLegacy": round(log_loss(points, 0.0, 0.148), 6),
        },
        "projection": {
            "intercept": round(proj_intercept, 4),
            "scale": round(proj_scale, 4),
            "sigma": round(proj_sigma, 4),
            "sampleSize": proj_n,
            "lookaheadHorizon": LOOKAHEAD_HORIZON,
            "carryoverShrink": CARRYOVER_SHRINK,
        },
        "calibration": rows,
        # Ratings the app uses to project weeks with no posted line: the most
        # recent completed season, regressed toward average for the offseason.
        "ratings": {t: round(v, 3) for t, v in latest.final_ratings.items()},
        "ratingsCarryover": {t: round(v, 3)
                             for t, v in carryover(latest.final_ratings).items()},
        "ratingsSeason": latest.season,
        "backtest": {"summary": summary, "exact": exact, "runs": backtests,
                     "strategies": list(strategies)},
        "history": [
            {
                "season": doc["season"],
                "weeks": [
                    {
                        "week": w["week"],
                        "byes": w["byes"],
                        "games": [
                            {"id": g["id"], "date": g["date"], "home": g["home"],
                             "away": g["away"], "homeScore": g["homeScore"],
                             "awayScore": g["awayScore"], "spread": g.get("spread"),
                             "favorite": g.get("favorite"), "provider": g.get("provider")}
                            for g in w["games"]
                        ],
                    }
                    for w in doc["weeks"]
                ],
            }
            for doc in seasons
        ],
    }

    json_path = os.path.join(DATA_DIR, "history.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)

    js_path = os.path.join(DATA_DIR, "history.js")
    with open(js_path, "w", encoding="utf-8") as fh:
        fh.write("// Generated by scripts/build_history.py — do not edit by hand.\n")
        fh.write("window.LOSERS_DATA = ")
        json.dump(payload, fh, separators=(",", ":"))
        fh.write(";\n")

    print(f"\nWrote {json_path} ({os.path.getsize(json_path) / 1024:.0f} KB)")
    print(f"Wrote {js_path} ({os.path.getsize(js_path) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
