Losers League Helper

Small Python CLI to help pick an NFL team most likely to lose each week in a "Losers League" (survivor-style) pool.

What it does
- Scrapes ESPN's public scoreboard API for the given NFL week/year.
- Converts point spreads to win/lose probabilities.
- Tracks which teams you have already used per profile/season.
- Suggests the best available team to pick to LOSE this week (and shows the top alternatives).
- Records your pick and, after games finish, records results and automatically consumes an insurance save if configured.

Quick start
1) Create a virtualenv (optional) and install `requests`:
   pip install requests

2) Show suggestions for a week (no state changes):
   python -m losers_league --season 2025 --week 1 --profile main suggest

3) Record your pick (writes to `state/main_2025.json`):
   python -m losers_league --season 2025 --week 1 --profile main pick --team NYJ

4) After the week ends, record results automatically from ESPN and update your save/elimination state:
   python -m losers_league --season 2025 --week 1 --profile main settle

Profiles and save
- Use `--profile` to keep separate entries (e.g., `main` and `alt`).
- Configure an insurance save with `--saves 1` when you first create a profile or via `init`:
   python -m losers_league --season 2025 --profile main init --saves 1

Notes
- If odds/spreads are missing for a game, the tool falls back to 50/50 for that matchup.
- The selection strategy currently picks the unused team with the highest probability to lose this week. A simple lookahead heuristic can be added later.
- Time/deadlines: If you plan to pick a team playing Thursday, be mindful of earlier deadlines per your league rules.

State files
- Stored under `state/{profile}_{season}.json`.
- Contains used teams, picks by week, save count, and elimination flag.

# losers-league-fantasy-algo
# losers-league-fantasy-algo
