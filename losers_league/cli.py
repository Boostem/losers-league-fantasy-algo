import argparse
import sys
from typing import List

from .espn import fetch_week_games, team_result_from_games
from .state import load_state, save_state, Pick
from .strategy import rank_loser_candidates, best_pick


def cmd_init(args: argparse.Namespace) -> int:
    ps = load_state(args.profile, args.season)
    if args.saves is not None:
        ps.saves_total = int(args.saves)
        # If this is a new profile or we are explicitly resetting saves, align left to total if it was zero
        if ps.saves_left == 0 or args.reset:
            ps.saves_left = ps.saves_total
    save_state(ps)
    print(f"Initialized profile '{ps.profile}' for season {ps.season}. Saves: {ps.saves_left}/{ps.saves_total}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ps = load_state(args.profile, args.season)
    print(f"Profile: {ps.profile} | Season: {ps.season}")
    print(f"Saves: {ps.saves_left}/{ps.saves_total} | Eliminated: {ps.eliminated}")
    print(f"Used teams ({len(ps.used)}): {', '.join(sorted(ps.used)) if ps.used else '-'}")
    if ps.picks:
        print("Picks:")
        for wk in sorted(ps.picks.keys(), key=lambda x: int(x)):
            p = ps.picks[wk]
            opp = f" vs {p.opponent}" if p.opponent else ""
            print(f"  Week {p.week}: {p.team}{opp} => {p.result}")
    return 0


def _ensure_week_args(args: argparse.Namespace) -> None:
    if args.week is None or args.season is None:
        print("Please provide --season and --week", file=sys.stderr)
        sys.exit(2)


def cmd_suggest(args: argparse.Namespace) -> int:
    _ensure_week_args(args)
    ps = load_state(args.profile, args.season)
    games = fetch_week_games(args.season, args.week)
    cands = rank_loser_candidates(games, used=ps.used)
    pick = best_pick(cands, used=ps.used)
    print(f"Week {args.week} suggestions (unused teams ranked by lose prob):")
    shown = 0
    from datetime import datetime
    import sys as _sys
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+
    except Exception:  # pragma: no cover
        ZoneInfo = None  # type: ignore

    def _local_dt(s: str) -> datetime:
        try:
            s2 = s.replace("Z", "+00:00")
            d = datetime.fromisoformat(s2)
            if d.tzinfo is None:
                # Assume UTC if missing tz
                if ZoneInfo:
                    d = d.replace(tzinfo=ZoneInfo("UTC"))
            # Convert to local timezone
            return d.astimezone()
        except Exception:
            return datetime.now().astimezone()

    def _dow_time_local(s: str) -> str:
        d = _local_dt(s)
        return d.strftime("%a %I:%M%p").replace("AM", "am").replace("PM", "pm")

    # Optional day filters
    avoid_days = {x.strip().title()[:3] for x in (args.avoid_days or '').split(',') if x.strip()}

    for c in cands:
        if c["team"] in ps.used:
            continue
        # Filter by avoid_days using local DOW
        dow3 = _dow_time_local(c['start']).split()[0]
        if avoid_days and dow3 in avoid_days:
            continue
        shown += 1
        star = "*" if pick and c["team"] == pick["team"] else " "
        loc = "home" if c["home"] else "away"
        sp = c["spread_points"]
        sp_txt = f"fav {c['spread_favorite_abbr']} {sp}" if c["spread_favorite_abbr"] and sp is not None else "no spread"
        print(f"{star} {c['team']} vs {c['opponent']} ({loc}, {_dow_time_local(c['start'])}) | lose p={c['lose_prob']:.3f} | {sp_txt}")
        if shown >= args.top:
            break
    if not pick:
        print("No available teams to pick (all used?)")
        return 1
    print()
    print(f"Recommended pick: {pick['team']} (lose p={pick['lose_prob']:.3f}) vs {pick['opponent']}")
    return 0


def cmd_pick(args: argparse.Namespace) -> int:
    _ensure_week_args(args)
    team = args.team.upper()
    ps = load_state(args.profile, args.season)
    games = fetch_week_games(args.season, args.week)
    # Verify team is playing this week
    teams_this_week = {g["home_abbr"] for g in games} | {g["away_abbr"] for g in games}
    if team not in teams_this_week:
        print(f"Team {team} not found on schedule for week {args.week}", file=sys.stderr)
        return 2
    # Find opponent
    opponent = None
    for g in games:
        if g["home_abbr"] == team:
            opponent = g["away_abbr"]
            break
        if g["away_abbr"] == team:
            opponent = g["home_abbr"]
            break
    ps.used.add(team)
    ps.picks[str(args.week)] = Pick(week=args.week, team=team, opponent=opponent, result="pending")
    save_state(ps)
    print(f"Recorded pick: Week {args.week} -> {team} vs {opponent}. Used teams now: {len(ps.used)}")
    return 0


def cmd_settle(args: argparse.Namespace) -> int:
    _ensure_week_args(args)
    ps = load_state(args.profile, args.season)
    p = ps.picks.get(str(args.week))
    if not p:
        print(f"No recorded pick for week {args.week}.")
        return 1
    games = fetch_week_games(args.season, args.week)
    res = team_result_from_games(games, p.team)
    if res is None:
        print("Week not completed or result unavailable yet. Try later.")
        return 1
    p.result = res
    if res in ("win", "tie"):
        # Our picked team did not lose -> elimination unless we have a save
        if ps.saves_left > 0:
            ps.saves_left -= 1
            print(f"Pick failed ({res}). Consuming a save. Saves left: {ps.saves_left}")
        else:
            ps.eliminated = True
            print(f"Pick failed ({res}). No saves left. You are eliminated.")
    else:
        print("Pick succeeded (team lost). You advance.")
    save_state(ps)
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    _ensure_week_args(args)
    ps = load_state(args.profile, args.season)
    p = ps.picks.get(str(args.week))
    if not p:
        print(f"No recorded pick for week {args.week}.")
        return 1
    p.result = args.result
    if p.result in ("win", "tie"):
        if ps.saves_left > 0:
            ps.saves_left -= 1
            print(f"Pick failed ({p.result}). Consuming a save. Saves left: {ps.saves_left}")
        else:
            ps.eliminated = True
            print(f"Pick failed ({p.result}). No saves left. You are eliminated.")
    else:
        print("Pick succeeded (team lost). You advance.")
    save_state(ps)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="losers_league", description="NFL Losers League helper")
    p.add_argument("--profile", default="main", help="Profile name (supports multiple entries)")
    p.add_argument("--season", type=int, help="Season year (e.g., 2025)")
    p.add_argument("--week", type=int, help="NFL week number (regular season)")
    sub = p.add_subparsers(dest="cmd", required=True)

    # init
    pi = sub.add_parser("init", help="Initialize profile and saves")
    pi.add_argument("--saves", type=int, default=None, help="Total insurance saves")
    pi.add_argument("--reset", action="store_true", help="Reset saves_left to saves_total")
    pi.set_defaults(func=cmd_init)

    # status
    ps = sub.add_parser("status", help="Show profile state")
    ps.set_defaults(func=cmd_status)

    # suggest
    psug = sub.add_parser("suggest", help="Suggest best loser pick for the week")
    psug.add_argument("--top", type=int, default=10, help="Show top N suggestions")
    psug.add_argument("--avoid-days", default="", help="Comma-separated day abbreviations to skip (e.g., Thu,Sat)")
    psug.set_defaults(func=cmd_suggest)

    # pick
    pp = sub.add_parser("pick", help="Record your pick for the week")
    pp.add_argument("--team", required=True, help="Team abbreviation (e.g., NYJ)")
    pp.set_defaults(func=cmd_pick)

    # settle
    pset = sub.add_parser("settle", help="Record outcomes and update save/elimination state")
    pset.set_defaults(func=cmd_settle)

    # manual result
    pmr = sub.add_parser("mark", help="Manually mark result for a week (win/loss/tie)")
    pmr.add_argument("--result", required=True, choices=["win", "loss", "tie"], help="Outcome for your picked team")
    pmr.set_defaults(func=cmd_mark)

    return p


def main(argv: List[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
