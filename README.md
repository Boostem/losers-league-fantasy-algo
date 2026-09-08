# Losers League Helper

Picks the NFL team most likely to **lose** each week for a Losers League
(survivor-in-reverse) pool, using betting lines from ESPN's public API and a
win-probability curve fitted to five seasons of real results.

## Using it

Recommended — picks are saved to a real JSON file:

```
python3 scripts/serve.py        # then open http://localhost:8000
```

Or just open `index.html` directly (double-click it). Everything works the
same, except picks are stored only in that browser. The page tells you which
mode you are in.

The page fetches the current week's games and odds from ESPN, ranks every
available team by its chance of losing, and recommends one per entry. Click
**Lock in** (or a team's button on the board) to record a pick; once the games
finish, hit **Settle** to pull the final score and update saves and
elimination.

- **Multiple entries** are first-class. Each has its own used teams, saves and
  elimination state, and each week the tool recommends a *different* team for
  each one — see below for why that matters. Add, rename or remove entries at
  any time; the default is two.
- **League rules are configurable**: saves per entry (set on each entry card)
  and whether a tie counts as surviving.
- Filter out Thursday/Sunday/Monday games if your league's deadline makes them
  impractical.
- Past seasons (2021–2025) can be browsed too; the closing lines for those are
  baked into `data/history.js`, since ESPN strips odds from old scoreboards.

### Where picks are stored

With `scripts/serve.py` running, picks go to `state/losers_<season>.json` —
a plain file you can read, commit, or back up, written atomically with one
previous generation kept as `.bak`. They are mirrored into the browser as well,
so losing the server does not lose the season.

Opened directly from disk, there is no server to write to, so picks live in
that browser's `localStorage` only. That is per-browser and per-device: no sync
between laptop and phone, and clearing site data wipes it. Note also that
`file://` and `http://localhost:8000` are separate origins with separate
storage — pick one and stay with it, or use the server, which is the same file
either way. **Export JSON** / **Import JSON** move a season between machines,
and Import also reads state files written by the old Python CLI.

## Playing more than one entry

Two entries only help if they are pointed at different games. Put both on the
same team and, when that team wins, they both die together — the pair survives
with exactly the probability of a single entry. Split them and the chance that
at least one survives the week is `1 - (1-p₁)(1-p₂)`.

With week 1 of 2026 as an example: ARI at 79.2% and CLE at 76.7% give a 95.1%
chance at least one entry survives, against 79.2% if both sit on ARI. Over a
season the effect compounds — two independent entries roughly double the odds
that one of them goes the distance, while two identical entries are worth no
more than one.

So the tool never recommends the same team to two entries in the same week. It
tries each order of entries and keeps whichever assignment maximises the odds
that at least one survives, since each entry has its own list of spent teams. It
will still *let* you double up from the board if you want to, with a warning.

## What the historical data actually showed

`scripts/build_history.py` harvested every regular-season game from 2021–2025
(1,358 games with a closing spread) and fitted the curve that turns a point
spread into a win probability. Two findings are worth stating plainly, because
both are negative results:

**1. The original weighting was already right.** The tool previously hardcoded
`k = 0.148` in a logistic curve. Fitting `k` to the actual data gives
**0.1434** — a difference too small to matter. Brier score improves from
0.2119 to 0.2117. Whoever picked 0.148 picked well.

| Spread | Games | Favourite actually won | Fitted model | Old k=0.148 |
|-------:|------:|-----------------------:|-------------:|------------:|
| 0.5–2.5  | 150 | 52.7% | 55.2% | 55.3% |
| 2.5–3.5  | 280 | 57.7% | 59.1% | 59.4% |
| 3.5–6.5  | 463 | 64.0% | 64.6% | 65.0% |
| 6.5–7.5  | 130 | 76.5% | 72.0% | 72.7% |
| 7.5–10.5 | 190 | 73.4% | 76.5% | 77.2% |
| 10.5–14.5| 106 | 85.9% | 84.7% | 85.4% |
| 14.5+    |  36 | 97.2% | 90.2% | 90.8% |

**2. Looking ahead does not help — planning ahead actively hurts.** The
appealing idea is to hold back a team that will be an even bigger underdog in
three weeks. It was tested three ways:

| Strategy | Season survival | Real seasons survived | Mean weeks |
|---|---:|---:|---:|
| Pick the biggest underdog (what the tool does) | **10.7%** | 1 / 5 | 10.2 |
| Hold teams back for better weeks | 10.8% | 1 / 5 | 10.2 |
| Plan every remaining week at once (optimal assignment) | 9.6% | 0 / 5 | 7.8 |

Survival here is computed *exactly*, not simulated: none of these strategies
look at results, only at the week and which teams are spent, so each one's
sequence of picks is fixed in advance and its survival probability follows from
a Poisson-binomial DP. There is no sampling noise in those numbers.

The reason planning loses is that future weeks have no posted line, so it has
to plan against projected ones — and projections carry about **±3.7 points** of
residual scatter against the number books eventually post. Trading a known edge
today for a guessed one later is a bad trade. So the tool stays myopic and just
takes the biggest available underdog. Teams that project better later are still
flagged on the board, but only as context.

Either way, expect to go out: with one save, surviving a full 18 weeks is about
a 1-in-9 proposition.

## Rebuilding the data

Only needed once a season is complete, to fold it into the fit:

```
python3 scripts/build_history.py                    # 2021-2025 by default
python3 scripts/build_history.py --seasons 2026     # add a finished season
```

It writes `data/history.json` (canonical, also read by the Python module) and
`data/history.js` (the same payload as `window.LOSERS_DATA`, so `index.html`
works from `file://`). Raw API responses are cached under `data/cache/`, which
is gitignored, so re-runs are cheap and resumable. Stdlib only — no
dependencies.

## Notes on ESPN's API

Three things cost real debugging time and are worth recording:

- The scoreboard endpoint **ignores `year=`** and always serves the current
  season. The parameter it honours is `dates=`. The original CLI used `year=`,
  so every historical query silently returned current-season games.
- `site.api.espn.com` sits behind bot protection that **403s unrecognised
  User-Agent strings**, including browser-looking ones sent by a non-browser.
  Sending no custom User-Agent works; setting a "polite" one does not.
- The odds list mixes closing lines with **in-game "Live Odds" feeds** and with
  model projections (accuscore, teamrankings, numberfire). Using the first
  entry blindly can pick up a number from the middle of a blowout, so those
  sources are filtered out.

## The old CLI

`losers_league/` is the original Python CLI. It still works and has been fixed
(the `dates=` bug, the User-Agent 403, the live-odds filter, and it now reads
the fitted curve from `data/history.json`):

```
python3 -m losers_league --season 2026 --week 1 --profile main suggest
python3 -m losers_league --season 2026 --week 1 --profile main pick --team ARI
python3 -m losers_league --season 2026 --week 1 --profile main settle
```

It stores state in `state/{profile}_{season}.json`, which the web app can
import. It has no notion of multiple entries — the web app is the one to use
for that. Note also that ESPN serves no odds for completed seasons, so the CLI
shows "no spread" for past weeks; the web app covers that from its archived
data.
