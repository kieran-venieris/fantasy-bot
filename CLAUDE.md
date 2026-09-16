# Fantasy manager — CLAUDE.md (read at the START of every run, update at the END)

Author: Cole. Everything below is genericized — "the owner" is whoever runs this fork,
teams are "Team N", and league identity comes from $LEAGUE_ID / $TEAM_ID in .env.

You are the full manager of the owner's ESPN team. The owner reviews briefs after the fact; they do not
approve moves in advance. Scheduled runs are Sonnet at medium effort (owner, 09-15, usage cut); no
Opus, no subagents. News searches only per rule 11. Never git push. Never guess a write endpoint.

## League: <your league name> (ESPN id from $LEAGUE_ID, 2026)
- 16 teams, snake draft, no keepers. 14 regular-season weeks, **6 make playoffs**, 1-week rounds
  (weeks 15-17). Tiebreak: total points scored.
- Scoring: full PPR. Pass TD 4, 0.04/pass yd, INT -2, rush/rec TD 6, 0.1/yd, fumble -2,
  2pt 2, D/ST 0 pts allowed = 5, sack 1, FG 50+ = 5.
- Lineup: QB, RB, RB, WR, WR, TE, FLEX (RB/WR/TE), K, D/ST, 7 bench, 1 IR.
- Waivers: traditional order (order resets), 24h claim window, processed every day except
  Tuesday. Game-time lock per player.
- Trades: 24h league review, **7 vetoes** to kill, no trade limit, deadline 2026-12-02 (Wed 09:00 PT).
- My team: id 4, Team 4. Team 13 "CPU Team 1" is a bot.

## Strategy (the owner's words, 2026-09-13)
- Win the title, not just make playoffs. 6 of 16 make it; a 12-2 floor team that scores 100/wk
  loses in week 15. Optimise for ceiling in weeks 15-17.
- Favour upside over floor. Prefer the player with the higher 80th-percentile outcome when
  medians are close.
- RB scarcity matters in 16 teams. Startable RBs are the currency; hoard them, trade WR depth
  for them, and never let RB count fall below what the lineup + one injury needs.

## Rules (2026-09-13 revision — these replace the "never drop starters / never trade core six" rules)
1. **Every move must raise projected rest-of-season (ROS) roster value** (owner, 09-13 09:41),
   computed by `transact.py value`: ESPN season proj minus points already scored; the optimal
   9-slot starting lineup counts at **1.0**, and the best bench RBs and WRs beyond the starters count
   at **0.3 each, capped at two bench RBs and two bench WRs** (QB/TE/K/DST depth counts 0).
   **State the number in the brief**, and the lineup-only Δ next to it. The scripts refuse a move
   with Δ ≤ 0 unless `--force` is passed with a reason; the routine never uses --force. Rules 3 and 6
   use this same Δ.
2. Dropping a current starter requires the replacement to project higher ROS than the dropped player.
3. Trading a **core** player (Allen, Hall, A. Jones, D. Smith, J. Williams, Warren) requires
   Δ ≥ +15 ROS lineup points AND a balanced roster afterwards: **≥5 RB and ≥5 WR**.
4. Adds: `add --waiver` unless state.json shows the player as a free agent. Only drop bench players
   unless rule 2 is met. Max 2 per run. No 3rd QB / 2nd K.
5. Trades: **max 3 proposals per NFL week** (manual ones count — grep log.md). Before ANY trade run
   `transact.py pending` and read state.json `pending_transactions`; skip if a proposal already
   involves my team and the target. Never withdraw a proposal the owner sent by hand.
6. **Incoming trades** (sent TO team 4): every run, `transact.py incoming`. Accept if it clears
   rule 1/3, reject if not, counter (`incoming` prints one-player-swap candidates) if a single
   swap would make it clear. `respond --action accept|reject --id <id> --live` does it
   (packet taken from ESPN's client bundle, see below). **Autonomous accept (owner, 09-13 09:40)
   is allowed only when Δ ≥ +15, the roster keeps ≥5 RB and ≥5 WR, and any drops needed for
   roster room are bench players** — the script enforces all three; never `--force` in the
   routine. After a live accept or reject, `respond` looks up the EXECUTED TRADE_ACCEPT /
   TRADE_DECLINE record in mTransactions2: put that id + status in log.md and in the brief.
7. Lineup: set the optimal lineup every run (`lineup --live`). Never bench an ACTIVE player for a
   lower-projected one; move OUT/DOUBTFUL/IR to BE before lock; skip locked players.
8. Any non-200: log the response text, no retry of that action until the next calendar day.
9. Log every executed transaction to log.md with timestamp and ESPN id.
10. Every brief ends with **"Decisions I made that you might disagree with"**.
11. **News searches (owner, 09-15):** web-search only the players `python3 search_targets.py` prints:
    my rostered players tagged QUESTIONABLE/DOUBTFUL/OUT, plus the top 5 ROS free agents at my two
    weakest positions (QB/RB/WR/TE starters ranked against the league). **Max 10 per run**, one
    WebSearch each, no WebFetch, no subagents. Never search healthy starters; use ESPN's tag and
    projections for them. The run's search count shows in launchd.log's `tokens:` line.

## `respond` packet — how it was obtained (2026-09-13)
The Chrome-extension network capture failed (it logged only heartbeats and never shows request
bodies), so the packet was read from ESPN's own web client instead of guessed: kona bundle
`main-82d208d52efd2b467c49.js`, functions `declineTrade` / `acceptTrade` and the transaction
class `get()` serialiser. It POSTs to the same `/transactions/` URL as every other move.
- decline: `{isLeagueManager:false, teamId:4, type:"TRADE_DECLINE", memberId:SWID, scoringPeriodId,
  executionType:"EXECUTE", comment:null, relatedTransactionId:<proposal id>}`
- accept: same with `type:"TRADE_ACCEPT"`, no comment, plus `items:[{playerId,type:"DROP",fromTeamId:4}]`
  only if the roster would overflow.
Cross-check: the record ESPN stored for the owner's manual decline (id ce8c8e47) is exactly
TRADE_DECLINE / EXECUTE / relatedTransactionId 479e3b3b / teamId 4 / scoringPeriodId 1.
**Status: dry-run verified (replayed team 9's offer: accept refused at Δ −182, reject packet
built), not yet used live.** `respond --live` prints the ESPN response id and then the stored
TRADE_DECLINE/TRADE_ACCEPT record (id + status). If ESPN returns non-200, log the body, do not
retry until the next day, and say in the brief that the owner needs to click it.
If a future capture is ever needed, have the owner use DevTools themselves (F12 → Network → filter
`lm-api-writes` → Preserve log → click → right-click request → Copy as cURL) — the extension
cannot see request bodies.

## Decision log (why, not just what)
- **09-12 22:54** First brief, recommend-only. Flagged Hall groin, Jones snap cap, Worthy shoulder.
- **09-13 08:15** Lineup already optimal on ESPN weekly proj (FLEX M. Wilson 10.46 > Worthy 9.52).
  No adds: no FA RB/WR cleared the then-rule of +20 season proj over any bench drop. Skipped
  backup QB/K that technically cleared it — dead roster slots.
- **09-13 08:20** Sent Worthy + Corum for Judkins to team 8 (RB surplus: Achane, C. Brown,
  Judkins at flex, Dobbins + Mason idle; thin WR). Reason: Judkins (227 proj) is a locked-in RB1
  workload; Worthy is my 4th WR and Corum a backup. **Mistake:** The owner had sent the identical
  offer at 08:09 (id d3f28f1d). Mine was a duplicate → withdrawn 08:31 (cancel id 308dcc23).
  Fix: pending-transaction guard on `trade` and `add`; `pending`/`withdraw` commands.
- **09-13 08:08** (owner, manual) Waiver claims: Kaleb Johnson/Sadiq, Emanuel Wilson/J. Johnson,
  Mooney/Douglas — the 09-12 brief's three adds. Pending; process Monday.
- **09-13 ~09:00** Promoted to full manager; rules above replace the old hard bans. Core-six
  definition kept as the list the owner originally protected. Balance floor is 5 RB / 5 WR; my roster
  is at **3 RB today** (Hall, Jones, Corum), so any core trade must be RB-positive until the
  pending RB waivers land.
- **09-13 08:53 incoming** Team 9 offered Shipley + Croskey-Merritt + Okonkwo + Mahomes for Hall +
  D. Smith. `incoming`: Δ −182.19 → REJECT. Only counter that clears the bar is Gibbs for
  Hall + Smith (Δ +31), which they will not do. Verdict: reject outright once `respond` exists.
- **09-13 09:05** Valuation check of what is already pending: Judkins deal Δ +49.75 (clears);
  the three manual waiver claims are bench-only moves, Δ 0.00 each — rule 1 as written would
  refuse them. Flagged to the owner; not withdrawn (they are the owner's).
- **09-13 09:27** The owner declined team 9's offer by hand in Chrome (ESPN id ce8c8e47) while I
  tried to capture the packet. Capture missed; packet recovered from the client bundle instead
  (see "`respond` packet" above). `respond` now exists; still dry-run-verified only.
- **09-13 09:40** The owner authorised autonomous accepts via `respond --live` at Δ ≥ +15 with roster
  floors and bench-only drops. Encoded as ACCEPT_MIN_GAIN in transact.py; rejects need no bar.
- **09-13 09:41** Rule 1 changed by the owner: Δ is now lineup 1.0 + best-2 bench RB/WR at 0.3
  (BENCH_WEIGHT/BENCH_SLOTS in transact.py; `evaluate` also returns `lineup_delta`). Answers the
  09:05 open question — bench depth now counts. Re-scored the three manual waiver claims:
  Kaleb Johnson/Sadiq **Δ +2.36**, Emanuel Wilson/J. Johnson **Δ +3.13** (both clear only because
  the 2nd bench-RB slot is empty; ESPN season proj for the two RBs is 7.9 / 10.4), Mooney/Douglas
  **Δ −5.65** (Mooney ROS 80.8 < Hunter 103.0 < Douglas 121.9, so the bench-WR pair gets worse).
  Baseline value 2034.20 = lineup 1899.86 + bench 134.34 (Corum, Worthy, Douglas). Claims not
  touched — the owner asked to see the deltas first.
- **09-13 20:45 (launchd run, after Sunday games)** Lineup: all 9 starters locked, no moves. No
  incoming. Baseline now 1925.34 (lineup 1797.53 + bench 127.81). **Waiver** Demercado/−Hunter
  (9975f6bf, Δ +24.58): fills empty 2nd bench-RB slot; Hunter moved mostly to CB in Wk1. Kept Cam
  Ward (same Δ) as Allen backup. **Trade** Wilson → Rachaad White to team 3 (2a9dfcb7, Δ +28.09):
  picked for acceptance odds (team 3 has 0 bench WR). Not sent: Wilson→Hubbard CPU 13 (+31.18;
  CPU teams can't accept trades), Wilson→Tuten team 17 (+56.05, lineup +24.49 — **first Week 2
  proposal candidate**). Week 1 proposals now 3/3. The owner's claims re-scored: K. Johnson +2.37 (0 off.
  snaps Wk1), E. Wilson +3.14 (2 carries), Mooney/Douglas **−1.89** (Douglas 5/94, MIA starter) —
  recommended the owner cancel; untouched. Corum 0 offensive snaps → Judkins deal better for us.
  **Watch:** if Judkins + White + all claims land, WR = Smith, Williams, Douglas, Mooney (3 if the
  Mooney claim fails) → add a WR (Coleman/Bryant FA) next run.
  **Bug fixed in procedure:** `league_state.py` prints to stdout; piping it to `tail` left
  state.json 6h stale. Always `python3 league_state.py > /tmp/s.json && cp /tmp/s.json state.json`.
- **09-14 17:38 (launchd run, during MNF DEN@KC)** No transactions. Lineup locked (all starters
  played; Worthy on BE in MNF). No incoming. Baseline 1923.44 (lineup 1795.63 + bench 127.81).
  Adds: none clear rule 1 (Coleman/Bryant Δ 0; every droppable TE is already in one of the owner's claims).
  No trades: **Week 1 cap still 3/3 until MNF ends**. Waivers not yet processed (all 4 claims pending).
  Re-scored: Judkins d3f28f1d **Δ +2.47 / lineup +48.21** (small total because 2 bench bodies leave),
  White 2a9dfcb7 +28.09 (White is RB2 behind Croskey-Merritt, 7-22), **K. Johnson/Sadiq now Δ 0.00
  FAILS** (ESPN ROS 0, GB kick returner), Mooney/Douglas −1.89 FAILS — recommended the owner cancel both.
  **Week 2 plan (first run on/after Tue 09-15):** refresh, then send in order, skipping any whose
  Δ no longer clears: (1) Ward + Douglas → Etienne, team 6 (**+108.54**, lineup +59.24). Team 6's only
  QB is Kyler Murray (now MIN, concussion Wk1). Re-check WR count first: if the pending trades land,
  WR falls to 3-4, so re-score with Douglas kept out. (2) Ward → Woody Marks, team 17 (+38.08).
  (3) Keep one slot open for a counter. Ward → Mitchell (+27.92) / Godwin (+15.32) are fallbacks.
  Selling Ward leaves Allen with no backup. Geno Smith (FA, ROS ~231) would score Δ 0 because QB
  depth counts 0, so rule 1 refuses the add. Flag it to the owner; don't force it.
  News corrections: A. Jones "IR" headline is a stale 2025 story; Judkins Q Wk2 but expected to play.
- **09-15 1126 (launchd run, Week 2)** Lineup fix: Worthy (10.93 proj) was benched behind Wilson
  (10.02) in FLEX — swapped, `lineup --live` id e30d4fa2. Judkins trade d3f28f1d (team 8) **expired
  unactioned** 08:19:59 PT — never responded, roster unaffected. **Trade sent:** Ward + Douglas →
  Etienne, team 6 (Team 6), id d6d95990, Δ +79.64 (lineup +61.21) — team 6's only
  QB is Murray (QUESTIONABLE) and their Etienne sits behind a bell-cow Taylor with a Q handcuff
  already rostered, so Ward is a real upgrade for them. Week 2 proposals 1/3. Not sent: Ward→Marks/
  Mitchell/Godwin fallbacks (moot, Ward already offered). No adds: TeSlaa and Coleman (best signal
  from the 10 searches) both Δ −21.93 vs Worthy — still fails. No incoming. Wilson-for-White trade
  to team 3 (2a9dfcb7) still unanswered, expires today 20:44 PT. The owner's 3 manual waiver claims +
  Demercado/Hunter claim untouched, unprocessed (Tuesday = no ESPN waiver processing).

## Opponents — trading habits (thin data: pre-Week-1, zero completed trades league-wide)
Update this block every run from state.json `recent_activity` and `pending_transactions`.
- **9 Team 9** (RB5/WR3): sends lopsided quantity-for-quality offers (4-for-2 targeting
  my RB1 + WR1 within an hour of Week 1 kickoff). Declined 09-13 09:27 with no counter. Treat
  first offers as anchors; counter hard. Watch whether they re-offer — that tells us how badly
  they want Hall.
- **8 Team 8** (RB6/WR5): RB-rich, WR-thin after Waddle/Pittman. Has our
  Worthy+Corum-for-Judkins offer pending (d3f28f1d). Response time unknown yet.
- **13 CPU Team 1**: bot (ESPN autopilot, RB7). ESPN support: autopilot manages lineups/waivers
  but does **not** accept trades — don't spend proposals on it (Hubbard/Spears unreachable).
- **3 Team 3** (RB7/WR3, zero bench WR, Kamara OUT): has our Wilson-for-White offer
  (2a9dfcb7, sent 09-13 20:43). Response time unknown yet.
- **17 Team 17** (RB7/WR5, Charbonnet OUT), **6 Team 6** (RB6/WR6):
  RB surplus, WR-light — natural partners for WR-for-RB deals. Team 17's Tuten is the top target
  (won JAX lead role Wk1). 09-14: team 17 has only one QB (Lawrence) and Flowers is hamstring Q.
  **Team 6's only QB is Kyler Murray** (MIN, ESPN QUESTIONABLE), Etienne is their RB2 behind a
  bell-cow Jonathan Taylor with a Q handcuff (Henderson) already on their bench, and Kelce/McLaurin
  are their other studs — sent them Ward + Douglas for Etienne 09-15 (id d6d95990, Δ +79.64,
  pending). Response time unknown yet.
- **16 Team 16** (Wk1 opponent): holds Daniels plus Darnold (ESPN DOUBTFUL), so not a QB
  buyer. Jacobs is ESPN DAY_TO_DAY.
- **14 Team 14** (RB5/WR5, deep WR bench) and **12 Team 12** (RB6/WR6): Brooks
  (+28.03) and Lloyd (+19.85) scored as Wilson swaps; neither team needs WRs.
- 09-13 20:45: recent_activity empty league-wide; no incoming offers since team 9's.
- 09-14 17:38: still empty. Teams 8 and 3 haven't answered after ~33h and ~21h. 3 and 8 are slow
  responders so far. Team 18 renamed to ".".
- **10 Team 10** (RB2/WR7), **11 Team 11** (RB3/WR8),
  **18 Team 18** (RB3/WR8): RB-starved — they are competitors for the same waiver RBs, and
  buyers if I ever sell an RB.
- Everyone else: no signal yet.

## Files
- `league_state.py` → **stdout**; redirect to state.json (roster, FAs, matchup, other teams, recent_activity, pending_transactions)
- `transact.py`: lineup / add / trade / pending / withdraw / value / incoming / respond. Dry run
  unless --live. `source .env` first.
- `search_targets.py`: reads state.json, prints the rule-11 search list (≤10).
- `run.sh` (launchd, Mon-Fri 08:04/21:04, Sun 09:04/11:04/21:04): Sonnet, effort medium; appends a
  `tokens:` line (per-model tokens, web searches, est. cost) to launchd.log after each run.
- `briefs/`, `log.md`, `implementation-notes.md` (engineering notes; this file holds the manager's memory).
