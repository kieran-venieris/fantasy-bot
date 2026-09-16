# fantasy-bot

An autonomous ESPN fantasy football manager driven by [Claude Code](https://claude.com/claude-code).
Built by Cole.

A scheduled run pulls the league state from ESPN, decides on lineup changes, waiver adds and trade
offers under a fixed set of rules, executes them against ESPN's write API, and writes a brief
explaining what it did and why. A local dashboard renders the briefs and pending moves.

This is a working setup, not a framework — it encodes one league's scoring, roster shape and
strategy. Treat it as a starting point to fork and edit.

## How it works

| File | Role |
| --- | --- |
| `league_state.py` | Reads the league via `espn_api` and prints a compact JSON snapshot (roster, free agents, matchup, other teams, recent activity, pending transactions) to **stdout**. |
| `transact.py` | Every write: `lineup`, `add`, `trade`, `pending`, `withdraw`, `value`, `incoming`, `respond`. **Dry run unless you pass `--live`.** |
| `search_targets.py` | Prints the short list of players worth a news search this run (injured rostered players + top free agents at the weakest positions). |
| `CLAUDE.md` | The manager's memory and rulebook: league settings, strategy, hard rules, and a decision log. Read at the start of every run, updated at the end. |
| `run-prompt.md` | The prompt handed to `claude -p` on each scheduled run. |
| `run.sh` | launchd/cron entry point. Checks auth, runs the session, logs token usage. |
| `dashboard/` | Stdlib-only HTTP server + single-page UI over `state.json`, `log.md` and `briefs/`. |
| `implementation-notes.md` | Engineering notes — how the ESPN write API was reverse-engineered, and why. |

The decision rules live in `CLAUDE.md`, not in code. `transact.py` enforces the numeric guardrails
(every move must raise projected rest-of-season roster value; trades of core players and autonomous
trade accepts need a bigger margin), and refuses a move that fails them unless `--force` is passed.

## Setup

```sh
git clone <your fork> && cd fantasy-bot
python3 -m venv .venv && source .venv/bin/activate
pip install espn-api requests

cp .env.example .env    # then fill in ESPN_S2, SWID, LEAGUE_ID, TEAM_ID
```

`ESPN_S2` and `SWID` are cookies from a logged-in browser session (DevTools → Application → Cookies
→ `fantasy.espn.com`). They are the credentials to your ESPN account — `.env` is gitignored, keep it
that way.

Then edit `CLAUDE.md` for your league: scoring, roster slots, waiver rules, your team id, your
strategy, and the `CORE` player set in `transact.py`.

## Use

```sh
set -a && source .env && set +a

python3 league_state.py > state.json          # refresh the snapshot
python3 transact.py value --to-team 8 --give "Player A" --get "Player B"   # score a hypothetical
python3 transact.py lineup --moves "Player A:FLEX" "Player B:BE"           # dry run
python3 transact.py lineup --moves "Player A:FLEX" "Player B:BE" --live    # execute
```

Every `transact.py` subcommand is a dry run until `--live`. Start there.

### Scheduled runs

`run.sh` is what launchd calls. Point a `~/Library/LaunchAgents/com.example.fantasy.plist` at it
with whatever `StartCalendarInterval` entries you want (cron works the same way on Linux). It
sources `.env`, verifies the CLI is authenticated, runs the session, and appends output plus a
`tokens:` line to `launchd.log`.

### Dashboard

```sh
python3 dashboard/server.py     # http://localhost:8787
```

Routes are whitelisted — it serves the UI and a JSON view built from `state.json`, `log.md`,
`briefs/` and `launchd.log`, and nothing else in the repo. It still binds `0.0.0.0` by default and
has no auth, so don't expose it to an untrusted network. The "Team N" names toggle masks real team
and owner names for screenshots.

## Caveats

- **ESPN's write API is undocumented.** It's the one the website itself uses, recovered from the web
  client (see `implementation-notes.md`). It can change without notice. `respond` in particular was
  verified by dry run and by cross-checking a stored ESPN record, not by long production use.
- **Check your league's rules.** Automated management may not be welcome in yours.
- Nothing here is affiliated with or endorsed by ESPN.

## License

MIT © Cole
