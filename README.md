# Losers League Helper

Picks the NFL team most likely to **lose** each week for a Losers League
(survivor-in-reverse) pool, using betting lines from ESPN's public API and a
win-probability curve fitted to five seasons of real results.

## Using it

Open `index.html` in a browser. That's the whole tool — no install, no build,
no server.

```
git clone <this repo>
open index.html          # or double-click it
```

If your browser blocks the local data file over `file://`, serve the folder
instead:

```
python3 -m http.server 8000     # then visit http://localhost:8000
```

The page fetches the current week's games and odds from ESPN, ranks every
available team by its chance of losing, and recommends one. Click a team (or
"Lock in") to record the pick; after the games finish, hit **Settle week** to
pull the final score and update your saves and elimination status.

- **Picks live in your browser only** (`localStorage`). Use **Export JSON** to
  back them up or move them to another device; **Import JSON** reads those
  files back, and also reads state files written by the old Python CLI.
- **League rules are configurable**: set the number of saves/mulligans and
  whether a tie counts as surviving. Defaults are 1 save and tie-survives.
- **Multiple profiles** are supported for running more than one entry.
- Filter out Thursday/Sunday/Monday games if your league's deadline makes them
  impractical.
- Past seasons (2021–2025) can be browsed too; the closing lines for those are
  baked into `data/history.js`, since ESPN strips odds from old scoreboards.

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
import. Note that ESPN serves no odds for completed seasons, so the CLI shows
"no spread" for past weeks; the web app covers that from its archived data.
