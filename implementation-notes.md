# implementation-notes.md — fantasy brief routine

Spec: every 2h, pull league state, web-search injury/news for roster + top-10 FA RB/WR,
write a <250-word brief to briefs/YYYY-MM-DD-HHMM.md, append one line to log.md. Recommend only.

## 2026-09-12 22:45 — scheduling decision
- Scheduled as a **session-local cron** (job 797e7c27, `4 */2 * * *`, i.e. every 2h at :04).
  A cloud schedule was not viable: the task sources a local `.env` and runs a local Python
  script against the user's filesystem, which a cloud agent cannot reach.
- Consequences: the job dies when this Claude Code session exits and auto-expires after 7 days.
  TODO: for a durable schedule, either (a) a launchd plist that runs `claude -p "<prompt>"`,
  or (b) move league_state.py + .env into a cloud-reachable location and use /schedule.
- Minute :04 instead of :00 chosen to avoid the top-of-hour API pile-on (tool guidance).

## 2026-09-12 22:50 — league_state.py hung on first run
- First pull hung >10 min: process alive, ~0.7s CPU, one ESTABLISHED socket to an ESPN CDN IP.
  `espn_api` issues `requests` calls with no timeout.
- Fix applied (small, unrequested change): `import socket` + `socket.setdefaulttimeout(30)`
  at top of league_state.py. Re-run completed in ~5s. Syntax-checked via ast.parse.
- Pull now writes to a scratchpad temp file and `mv`s over state.json so a failed pull
  can't leave an empty state.json (the hung run had truncated it to 0 bytes).
  The cron prompt still uses the plain `> state.json` form the user specified; a future
  run that hangs would be caught by the 30s timeout and exit non-zero instead.

## Research approach
- 36 player searches per run are fanned out to 3 parallel Sonnet subagents (roster / FA RB /
  FA WR) that return one compact line per player, keeping raw search results out of the
  main session context.
- Note: state.json `week` = 1 and `pct_owned` for FAs is very low across the board, which
  reads like a pre-Week-1 snapshot; recommendations lean on projections + news accordingly.

## 2026-09-13 08:15 — routine upgraded from recommend-only to execute
- Session-local cron from 09-12 was gone (session ended). Re-created with the new execute rules;
  still session-local, still 7-day expiry. Same launchd TODO as above stands.
- First run per spec: `lineup` run WITHOUT --live; packet pasted into the brief. Second run
  onward uses --live (baked into the cron prompt). The packet was generated with every player
  mapped to its current slot (all no-ops) because the lineup was already optimal; the command
  requires at least one --moves entry.
- Lineup optimality judged on ESPN **weekly** projections pulled via box_scores (state.json
  only carries season totals). FLEX: M. Wilson 10.46 over Worthy 9.52.
- Add rule interpretation: "+20 season proj over the drop" treated as a necessary gate, not a
  trigger. Backup QBs and kickers mechanically clear it vs Travis Hunter (103) but a 3rd QB /
  2nd K is dead roster space, so no add was executed. Only RB/WR/TE FAs are considered. TODO:
  confirm with user that this reading is intended.
- Trade executed: Worthy + Corum for Judkins to team 8, ESPN 200, id 62028bcc-…, PENDING.
  Corum was game-locked (played Thu) and ESPN accepted the proposal anyway. Weekly trade
  budget for Week 1 is now spent; next eligible trade is Week 2. Fallback (Worthy for Dobbins)
  only if this one is declined.
- "Once per week" for trades is tracked by grepping log.md for `TRADE_PROPOSAL` lines dated
  in the current NFL week; the cron prompt tells future runs to do that.
- Non-200 cooldown ("don't retry until next day") is also tracked via log.md — a future run
  must grep for `ESPN <4xx|5xx>` lines dated today before retrying an action.

## 2026-09-13 08:31 — duplicate trade proposal, withdrawn
- The owner had manually sent Worthy+Corum-for-Judkins to team 8 at 08:09 (id d3f28f1d). The routine
  sent an identical one at 08:20 (id 62028bcc) because it never checked pending transactions.
  Withdrawn at 08:31 (ESPN 200, cancel id 308dcc23). The owner's proposal is the live one.
- ESPN cancel mechanics (undocumented): POST /transactions/ with type TRADE_PROPOSAL,
  executionType CANCEL, and the target id in `relatedTransactionId`. DELETE → 405; id in `id` →
  409 TRAN_NOT_FOUND.
- Changes: league_state.py now emits `pending_transactions` (mPendingTransactions view).
  transact.py gained `pending` and `withdraw --id` subcommands, and `trade` now refuses to send
  if any PENDING trade already involves both my team and the target team.
- Cron prompt updated: before any trade, run `transact.py pending`, read state.json
  `pending_transactions` + `recent_activity`, and skip if a proposal to that team exists.
- Also found 3 PENDING WAIVER claims by team 4 submitted manually at ~08:08 (the 09-12 brief's
  recommended adds). `add` now refuses when a pending waiver already touches the add or drop
  player, so the routine can't double-claim. `pending` lists all pending types.

## 2026-09-13 09:05 — promoted to full manager
- CLAUDE.md added (manager memory: settings, strategy, rules, decision log, opponent notes).
  Cron prompt starts with `cat CLAUDE.md` and ends by updating it.
- transact.py: `ros()` = ESPN season proj − points scored; `lineup_points()` = optimal 9-slot
  lineup; `evaluate()` / `clears_bar()` enforce rule 1 (Δ>0), rule 2 (starter drop needs a
  higher-ROS replacement), rule 3 (core out ⇒ Δ≥15 and ≥5 RB/≥5 WR). Old "never drop a
  starter" die() removed. `--force REASON` exists for manual overrides; the cron prompt forbids it.
- New: `value` (read-only Δ), `incoming` (scores proposals sent to us + one-player-swap counter
  search over both rosters), `respond` (stub — dies until the accept/reject packet is captured
  from Chrome; TODO(capture) marker in code and procedure in CLAUDE.md).
- Consequence to flag: rule 1 measures the starting lineup only, so pure bench-depth adds
  (Mooney over Douglas, Kaleb Johnson over Sadiq) score Δ 0 and the script refuses them. That
  conflicts with the "hoard RBs" strategy. Left as-is; the owner to decide whether bench value should
  count (e.g. Δ of the top-N bench by position).
- Trade deadline is 2026-12-02 09:00 PT (ms 1796230800000), not Nov 30 as first written.
- Weekly trade cap (3) is enforced by the prompt via log.md grep, not by code. TODO: move into
  `trade` once log.md has a stable machine-readable format.
- Model: the cron fires inside this session, so the reasoning step already runs on the session
  model (Fable 5.1); the per-project instruction overrides the global "default Sonnet" rule.

## 2026-09-13 09:30 — `respond` implemented (accept / reject incoming trades)
- **Capture attempt failed.** The owner's own ESPN tab is invisible to the Claude-in-Chrome extension
  (it only sees tabs in its own group), so I opened the trade review page in a new extension
  window, armed `read_network_requests` (confirmed live via a heartbeat), and the owner clicked Decline
  there. The POST never appeared — only presence heartbeats and ad pings — and the tool reports
  url/method/status only, no bodies. It could not have satisfied "exact format" even on success.
- **Packet source instead:** ESPN's client bundle (`main-82d208d52efd2b467c49.js`), functions
  `declineTrade`, `acceptTrade`, and the transaction class `get()` serialiser. This is the code
  that builds the request, not an inference from the CANCEL packet. Cross-checked against the
  stored record of the owner's decline (TRADE_DECLINE / EXECUTE / EXECUTED, id ce8c8e47).
- `respond_payload()` is a pure function so the packet can be unit-checked without ESPN;
  `cmd_respond` guards: must be pending, must not be ours, must involve team 4; accept runs
  evaluate/enforce (rule 1/3), computes roster overflow against ROSTER_MAX=16 (IR excluded) and
  requires `--drop` for the excess; dropping a current starter needs `--force` (rule 2).
- Decline always sends `comment: null` and no dealbreaker items (client sends items only when the
  dealbreaker boxes are ticked). Trade comments are not supported; not needed by the routine.
- **Not yet used live.** Nothing to reject right now. TODO(verify-live): on first live call, check
  mTransactions2 for an EXECUTED TRADE_DECLINE/TRADE_ACCEPT and note it in CLAUDE.md.
- Tradeoff: I judged reading the client source acceptable under "don't guess the endpoint"; the owner
  should say if they want a true DevTools capture (F12 → Copy as cURL) before the first live use.

## 2026-09-13 09:40 — autonomous accept authorised
- The owner: `respond --live` may accept on its own at Δ ≥ +15, roster floors (≥5 RB / ≥5 WR) held,
  drops bench-only. Encoded as `ACCEPT_MIN_GAIN = 15.0` plus a floors check applied to every
  accept (stricter than rule 1's Δ > 0 for non-core trades — deliberate, per the owner).
- `post()` now returns the parsed ESPN response; `respond --live` prints the response id, then
  `verify_record()` pulls mTransactions2 and prints the stored TRADE_ACCEPT/TRADE_DECLINE id and
  status so the brief can cite the EXECUTED record. Warns instead of failing if not visible yet.
- Dry-run replay of team 9's canceled offer: accept refused (Δ −182, core), reject packet built.
- Cron 56d3513f replaced by a new session-only job with the full routine text incl. respond.

## 2026-09-13 09:41 — rule 1 rescored: bench RB/WR depth at 0.3
- The owner: adds/trades scored on ROS where starters count 1.0 and the best bench RB and WR beyond the
  starters count 0.3 each, capped at two bench RBs and two bench WRs.
- transact.py: `BENCH_WEIGHT = 0.3`, `BENCH_SLOTS = (("RB",2),("WR",2))`, new `bench_value()` and
  `roster_value()`; `evaluate()` now returns `delta` on roster value plus `lineup_delta`,
  `bench_before/after` and the bench names counted; `report()` prints both lines. `clears_bar`,
  `enforce`, `add`, `trade`, `incoming`, `respond` all inherit the new Δ unchanged — rule 3 and the
  autonomous-accept bar (both ≥ +15) are now measured on roster value, not lineup only.
- Assumption: "bench" = every rostered player outside the optimal 9 lineup, including anyone in the
  IR slot (no one is on IR today, so it makes no difference yet). TODO: exclude IR-slotted players
  from bench value if the owner wants — one-line filter in `bench_value`.
- Assumption: QB/TE/K/D-ST bench depth still counts 0 (the owner named RB and WR only).
- Rule 2 (starter drop needs higher-ROS replacement) still compares raw ROS, not weighted value.
- Result: two of the owner's three pending claims now clear with small positive Δ (+2.36, +3.13) purely
  because the roster has one bench RB (Corum) and the second bench-RB slot is empty; the Mooney
  claim goes negative (−5.65). Nothing withdrawn; the owner asked for the numbers first.

## 2026-09-13 14:40 — The host machine is now the only scheduler (launchd)
- **The mini is the permanent and only scheduler.** Resolves the launchd TODO above. The laptop's
  session-local cron is gone: its session ended, and `CronList` there returns "No scheduled jobs".
  Cloud routines (RemoteTrigger) hold only the two School Memoria jobs. Do not recreate a session
  cron anywhere; two schedulers would both make live moves.
- `~/Library/LaunchAgents/com.example.fantasy.plist` (label `com.example.fantasy`): StartCalendarInterval
  at minute 4 of hours 0,2,…,22, RunAtLoad false, stdout/stderr to `launchd.log`.
- `run.sh`: sets PATH (launchd's is minimal; claude is in ~/.local/bin, python3 is
  /opt/homebrew/bin 3.14 and has espn_api + requests), cd's here, sources `.env` with `set -a`, and
  runs `claude -p "$(cat run-prompt.md)" --dangerously-skip-permissions >> launchd.log 2>&1` between
  timestamped start/end lines that include the exit code. Exits 1 without running if run-prompt.md
  is missing or empty.
- `run-prompt.md`: the laptop cron's last prompt, **verbatim** (created 09:36 PDT, the "cron prompt
  replaced" entry in log.md). Recovered from that session's transcript on the laptop and pasted in.
- Known drift: the prompt predates the 09:41 rule-1 change, so it still says "Δ ROS lineup" where
  the rule is now roster value (lineup 1.0 + best-2 bench RB/WR at 0.3). The prompt tells the run to
  follow CLAUDE.md, and transact.py enforces the new Δ, so behaviour is correct. The text was left
  verbatim on purpose; edit run-prompt.md if the wording should match.
- The routine commits locally in the ~/tools repo at the end of each run. `.gitignore` now also
  ignores `state.json` and `launchd.log` so those never get committed.
- Model: `claude -p` uses the CLI's default model on the mini, not a chosen session model.
