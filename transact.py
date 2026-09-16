#!/usr/bin/env python3
"""
transact.py — perform ESPN fantasy transactions (lineup, add/drop, trade offer).

This talks to ESPN's undocumented write API (the one the website uses).
Everything is DRY RUN unless you pass --live.

Usage examples:
    python3 transact.py lineup --moves "Blake Corum:RB" "Xavier Worthy:FLEX" "Breece Hall:BE"
    python3 transact.py add --add "Kaleb Johnson" --drop "Kenyon Sadiq" --live
    python3 transact.py trade --to-team 8 --give "Xavier Worthy" "Blake Corum" --get "Quinshon Judkins" --live
    python3 transact.py pending
    python3 transact.py value --to-team 8 --give "Xavier Worthy" "Blake Corum" --get "Quinshon Judkins"
    python3 transact.py incoming
    python3 transact.py withdraw --id <espn-transaction-id> --live

Env: ESPN_S2, SWID, LEAGUE_ID, TEAM_ID (required); YEAR (optional). See .env.example.
"""
import argparse
import json
import os
import socket
import sys

import requests
from espn_api.football import League

socket.setdefaulttimeout(30)

LEAGUE_ID = int(os.environ["LEAGUE_ID"])
YEAR = int(os.environ.get("YEAR", "2026"))
MY_TEAM_ID = int(os.environ["TEAM_ID"])
ESPN_S2 = os.environ.get("ESPN_S2")
SWID = os.environ.get("SWID")

READ_URL = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{YEAR}"
            f"/segments/0/leagues/{LEAGUE_ID}")
WRITE_URL = (f"https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons/{YEAR}"
             f"/segments/0/leagues/{LEAGUE_ID}/transactions/")

# ESPN's numeric ids for lineup slots
SLOT = {"QB": 0, "RB": 2, "WR": 4, "TE": 6, "FLEX": 23, "D/ST": 16, "K": 17, "BE": 20, "IR": 21}
SLOT_NAME = {v: k for k, v in SLOT.items()}
SLOT_NAME[3] = "RB/WR"; SLOT_NAME[7] = "RB/WR/TE"

# Manager rules (see CLAUDE.md). Core players may be traded, but only for a big gain
# and only if the roster stays balanced. Edit CORE for your own roster.
CORE = {"Josh Allen", "Breece Hall", "Aaron Jones Sr.", "DeVonta Smith", "Jameson Williams", "Tyler Warren"}
CORE_TRADE_MIN_GAIN = 15.0
ACCEPT_MIN_GAIN = 15.0   # respond --live may accept on its own only at Δ ≥ +15
MIN_RB, MIN_WR = 5, 5
LINEUP_SLOTS = (("QB", 1), ("RB", 2), ("WR", 2), ("TE", 1), ("K", 1), ("D/ST", 1))
FLEX_POS = ("RB", "WR", "TE")
# Rule 1 (2026-09-13 09:41): moves are scored on ROS *roster value* = starting-lineup points
# at 1.0 plus the best bench RBs and WRs beyond the starters at 0.3 each, capped at two of each.
BENCH_WEIGHT = 0.3
BENCH_SLOTS = (("RB", 2), ("WR", 2))


def ros(p):
    """Rest-of-season projection = ESPN season projection minus points already scored."""
    proj = getattr(p, "projected_total_points", 0) or 0
    pts = getattr(p, "total_points", 0) or 0
    return max(float(proj) - float(pts), 0.0)


def lineup_points(players):
    """(total, starter_ids) for the optimal QB/RB/RB/WR/WR/TE/FLEX/K/DST lineup by ROS."""
    by = {}
    for p in players:
        by.setdefault(p.position, []).append(p)
    for k in by:
        by[k].sort(key=ros, reverse=True)
    total, used = 0.0, set()
    for pos, n in LINEUP_SLOTS:
        for p in by.get(pos, [])[:n]:
            total += ros(p); used.add(p.playerId)
    flex = [p for pos in FLEX_POS for p in by.get(pos, []) if p.playerId not in used]
    if flex:
        f = max(flex, key=ros); total += ros(f); used.add(f.playerId)
    return round(total, 2), used


def pos_counts(players):
    c = {}
    for p in players:
        c[p.position] = c.get(p.position, 0) + 1
    return c


def bench_value(players, starters):
    """0.3 x ROS of the best 2 bench RBs and best 2 bench WRs (players outside the optimal lineup).
    Returns (weighted_total, [names counted])."""
    total, counted = 0.0, []
    for pos, n in BENCH_SLOTS:
        bench = sorted((p for p in players if p.position == pos and p.playerId not in starters),
                       key=ros, reverse=True)[:n]
        for p in bench:
            total += BENCH_WEIGHT * ros(p); counted.append(p.name)
    return round(total, 2), counted


def roster_value(players):
    """Rule 1 score: lineup ROS at 1.0 + weighted bench RB/WR depth. Returns a dict."""
    lineup, starters = lineup_points(players)
    bench, names = bench_value(players, starters)
    return {"lineup": lineup, "bench": bench, "bench_names": names,
            "value": round(lineup + bench, 2), "starters": starters}


def evaluate(roster, incoming=(), outgoing=()):
    """Compare current ROS roster value vs. the value after a hypothetical move.
    'delta' is the rule-1 number (lineup 1.0 + bench 0.3); 'lineup_delta' is starters only."""
    out_ids = {p.playerId for p in outgoing}
    after = [p for p in roster if p.playerId not in out_ids] + list(incoming)
    b, a = roster_value(roster), roster_value(after)
    return {"before": b["value"], "after": a["value"], "delta": round(a["value"] - b["value"], 2),
            "lineup_before": b["lineup"], "lineup_after": a["lineup"],
            "lineup_delta": round(a["lineup"] - b["lineup"], 2),
            "bench_before": b["bench"], "bench_after": a["bench"],
            "bench_names_before": b["bench_names"], "bench_names_after": a["bench_names"],
            "counts": pos_counts(after)}


def clears_bar(ev, outgoing):
    """Rule 1 (Δ>0) plus rule 3 for core players (Δ≥15 and ≥5 RB / ≥5 WR afterwards).
    Returns (ok, reason)."""
    core_out = [p.name for p in outgoing if p.name in CORE]
    if core_out:
        if ev["delta"] < CORE_TRADE_MIN_GAIN:
            return False, f"core player(s) {core_out} out but Δ {ev['delta']:+} < +{CORE_TRADE_MIN_GAIN}"
        c = ev["counts"]
        if c.get("RB", 0) < MIN_RB or c.get("WR", 0) < MIN_WR:
            return False, f"core trade leaves roster unbalanced: RB {c.get('RB',0)}/{MIN_RB}, WR {c.get('WR',0)}/{MIN_WR}"
    elif ev["delta"] <= 0:
        return False, f"Δ {ev['delta']:+} does not raise ROS roster value (lineup 1.0 + bench 0.3)"
    return True, "ok"


def report(ev, label):
    print(f"ROS value {label}: {ev['before']} -> {ev['after']}  (Δ {ev['delta']:+})  "
          f"roster after: {ev['counts']}")
    print(f"  lineup {ev['lineup_before']} -> {ev['lineup_after']} (Δ {ev['lineup_delta']:+}); "
          f"bench@{BENCH_WEIGHT} {ev['bench_before']} -> {ev['bench_after']} "
          f"[{', '.join(ev['bench_names_after']) or 'none'}]")


def enforce(ev, outgoing, force, what):
    ok, why = clears_bar(ev, outgoing)
    if not ok:
        if force:
            print(f"WARNING: {what} fails the bar ({why}) — proceeding on --force: {force}")
        else:
            die(f"{what} refused: {why}. (Override with --force \"reason\" — manual use only.)")


def die(msg):
    sys.exit(f"ERROR: {msg}")


def league():
    if not (ESPN_S2 and SWID):
        die("ESPN_S2 / SWID env vars missing")
    return League(league_id=LEAGUE_ID, year=YEAR, espn_s2=ESPN_S2, swid=SWID)


def find_player(pool, name):
    """Case-insensitive name match. Fails loudly on 0 or 2+ matches so we never
    accidentally move the wrong guy."""
    hits = [p for p in pool if p.name.lower() == name.lower()]
    if not hits:
        hits = [p for p in pool if name.lower() in p.name.lower()]
    if len(hits) != 1:
        die(f"'{name}' matched {len(hits)} players: {[p.name for p in hits]}")
    return hits[0]


def post(payload, live):
    """Send the transaction to ESPN, or just print it if dry-run."""
    print(json.dumps(payload, indent=2))
    if not live:
        print("\n[DRY RUN] nothing sent. Add --live to execute.")
        return
    headers = {
        "Content-Type": "application/json",
        "X-Fantasy-Source": "kona",
        "X-Fantasy-Platform": "kona-PROD",
    }
    cookies = {"espn_s2": ESPN_S2, "SWID": SWID}
    r = requests.post(WRITE_URL, json=payload, headers=headers, cookies=cookies, timeout=30)
    print(f"\nESPN responded {r.status_code}")
    print(r.text[:1500])
    if r.status_code >= 400:
        sys.exit(1)
    try:
        return r.json()
    except ValueError:
        return None


# ---------- lineup ----------
def cmd_lineup(args):
    lg = league()
    me = next(t for t in lg.teams if t.team_id == MY_TEAM_ID)
    week = lg.current_week
    items = []
    for mv in args.moves:
        name, _, slot = mv.rpartition(":")
        if slot not in SLOT:
            die(f"unknown slot '{slot}' (use {list(SLOT)})")
        p = find_player(me.roster, name)
        cur = SLOT.get(p.lineupSlot, None)
        if cur is None:  # library gives the name; map odd flex names
            cur = {"RB/WR/TE": 23, "RB/WR": 3}.get(p.lineupSlot, 20)
        items.append({"playerId": p.playerId, "type": "LINEUP",
                      "fromLineupSlotId": cur, "toLineupSlotId": SLOT[slot]})
        print(f"{p.name}: {p.lineupSlot} -> {slot}")
    payload = {"isLeagueManager": False, "teamId": MY_TEAM_ID, "type": "ROSTER",
               "memberId": SWID, "scoringPeriodId": week, "executionType": "EXECUTE",
               "items": items}
    post(payload, args.live)


# ---------- add / drop ----------
def cmd_add(args):
    lg = league()
    me = next(t for t in lg.teams if t.team_id == MY_TEAM_ID)
    week = lg.current_week
    fa = lg.free_agents(size=400)
    add_p = find_player(fa, args.add)
    items = [{"playerId": add_p.playerId, "type": "ADD", "toTeamId": MY_TEAM_ID}]
    drop_p = None
    if args.drop:
        drop_p = find_player(me.roster, args.drop)
        items.append({"playerId": drop_p.playerId, "type": "DROP", "fromTeamId": MY_TEAM_ID})
        print(f"add {add_p.name} (ROS {ros(add_p):.1f}), drop {drop_p.name} (ROS {ros(drop_p):.1f})")
        _, starters = lineup_points(me.roster)
        if drop_p.playerId in starters and ros(add_p) <= ros(drop_p):
            die(f"{drop_p.name} is a current starter; replacement must project higher ROS (rule 2)")
    else:
        print(f"add {add_p.name} (ROS {ros(add_p):.1f})")
    ev = evaluate(me.roster, incoming=[add_p], outgoing=[drop_p] if drop_p else [])
    report(ev, "add/drop")
    enforce(ev, [drop_p] if drop_p else [], args.force, "add/drop")
    # Guard: don't duplicate a waiver claim we (or a human) already have pending for this add/drop.
    for t in pending_transactions("WAIVER"):
        if t.get("teamId") != MY_TEAM_ID:
            continue
        pids = {i.get("playerId") for i in t.get("items", [])}
        if add_p.playerId in pids or (args.drop and drop_p.playerId in pids):
            die(f"a WAIVER claim touching {add_p.name}{' / ' + drop_p.name if args.drop else ''} "
                f"is already PENDING (id {t['id']})")
    # WAIVER if the player is on waivers, otherwise FREEAGENT
    ttype = "WAIVER" if (args.waiver or getattr(add_p, "onWaivers", False)) else "FREEAGENT"
    payload = {"isLeagueManager": False, "teamId": MY_TEAM_ID, "type": ttype,
               "memberId": SWID, "scoringPeriodId": week, "executionType": "EXECUTE",
               "items": items}
    post(payload, args.live)


# ---------- pending transactions ----------
def pending_transactions(kind=None):
    """Live list of PENDING transactions in the league (from ESPN's read API)."""
    r = requests.get(READ_URL, params={"view": "mPendingTransactions"},
                     cookies={"espn_s2": ESPN_S2, "SWID": SWID}, timeout=30)
    r.raise_for_status()
    return [t for t in r.json().get("pendingTransactions", []) if kind is None or t.get("type") == kind]


def pending_trades():
    return pending_transactions("TRADE_PROPOSAL")


def cmd_pending(args):
    for t in pending_transactions():
        print(t["id"], t.get("type"), "team", t["teamId"], t.get("status"),
              [(i.get("type"), i["playerId"], i.get("fromTeamId"), i.get("toTeamId")) for i in t["items"]])


# ---------- trade proposal ----------
def cmd_trade(args):
    lg = league()
    me = next(t for t in lg.teams if t.team_id == MY_TEAM_ID)
    them = next((t for t in lg.teams if t.team_id == args.to_team), None) or die("bad --to-team")
    items, give, get = [], [], []
    for n in args.give:
        p = find_player(me.roster, n); give.append(p)
        items.append({"playerId": p.playerId, "type": "TRADE", "fromTeamId": MY_TEAM_ID, "toTeamId": them.team_id})
    for n in args.get:
        p = find_player(them.roster, n); get.append(p)
        items.append({"playerId": p.playerId, "type": "TRADE", "fromTeamId": them.team_id, "toTeamId": MY_TEAM_ID})
    ev = evaluate(me.roster, incoming=get, outgoing=give)
    report(ev, f"trade with team {them.team_id}")
    enforce(ev, give, args.force, "trade")
    # Guard: never duplicate a proposal that is already pending (human-submitted or a prior run).
    # 2026-09-13: the routine re-sent a Worthy+Corum-for-Judkins offer the owner had already made by hand.
    for t in pending_trades():
        involved = {t.get("teamId")} | {i.get("fromTeamId") for i in t["items"]} | {i.get("toTeamId") for i in t["items"]}
        if MY_TEAM_ID in involved and them.team_id in involved:
            die(f"a trade with team {them.team_id} is already PENDING (id {t['id']}); "
                f"withdraw it first or wait for a response")
    print(f"propose to {them.team_name}: give {args.give} for {args.get}")
    payload = {"isLeagueManager": False, "teamId": MY_TEAM_ID, "type": "TRADE_PROPOSAL",
               "memberId": SWID, "scoringPeriodId": lg.current_week, "executionType": "EXECUTE",
               "items": items, "relatedTransactionId": None}
    post(payload, args.live)


# ---------- withdraw a pending proposal ----------
def cmd_withdraw(args):
    """Cancel one of OUR pending trade proposals. ESPN wants the id in relatedTransactionId
    with executionType CANCEL (DELETE is 405; putting the id in "id" returns TRAN_NOT_FOUND)."""
    t = next((t for t in pending_trades() if t["id"] == args.id), None) or die(f"{args.id} is not a pending trade")
    if t.get("teamId") != MY_TEAM_ID:
        die(f"{args.id} was proposed by team {t.get('teamId')}, not us — refusing to touch it")
    print(f"withdraw {args.id}")
    payload = {"isLeagueManager": False, "teamId": MY_TEAM_ID, "type": "TRADE_PROPOSAL",
               "memberId": SWID, "scoringPeriodId": league().current_week, "executionType": "CANCEL",
               "relatedTransactionId": args.id, "items": []}
    post(payload, args.live)


# ---------- valuation ----------
def cmd_value(args):
    """Δ ROS roster value (lineup 1.0 + bench RB/WR 0.3) for a hypothetical move. Read-only."""
    lg = league()
    me = next(t for t in lg.teams if t.team_id == MY_TEAM_ID)
    incoming, outgoing = [], []
    if args.add:
        incoming.append(find_player(lg.free_agents(size=400), args.add))
    if args.drop:
        outgoing.append(find_player(me.roster, args.drop))
    if args.to_team:
        them = next((t for t in lg.teams if t.team_id == args.to_team), None) or die("bad --to-team")
        incoming += [find_player(them.roster, n) for n in (args.get or [])]
        outgoing += [find_player(me.roster, n) for n in (args.give or [])]
    ev = evaluate(me.roster, incoming, outgoing)
    report(ev, "hypothetical")
    ok, why = clears_bar(ev, outgoing)
    print("VERDICT:", "CLEARS BAR" if ok else f"FAILS — {why}")


# ---------- incoming trades ----------
def cmd_incoming(args):
    """List trade proposals sent TO us, score each, and search one-player-swap counters."""
    lg = league()
    teams = {t.team_id: t for t in lg.teams}
    me = teams[MY_TEAM_ID]
    found = False
    for t in pending_trades():
        if t.get("teamId") == MY_TEAM_ID:
            continue
        if not any(i.get("toTeamId") == MY_TEAM_ID or i.get("fromTeamId") == MY_TEAM_ID for i in t["items"]):
            continue
        found = True
        them = teams.get(t["teamId"])
        by_id = {p.playerId: p for p in me.roster + (them.roster if them else [])}
        get = [by_id[i["playerId"]] for i in t["items"] if i.get("toTeamId") == MY_TEAM_ID and i["playerId"] in by_id]
        give = [by_id[i["playerId"]] for i in t["items"] if i.get("fromTeamId") == MY_TEAM_ID and i["playerId"] in by_id]
        print(f"\nINCOMING {t['id']} from team {t['teamId']} ({them.team_name.strip() if them else '?'})")
        print(f"  they give: {[(p.name, round(ros(p),1)) for p in get]}")
        print(f"  they want: {[(p.name, round(ros(p),1)) for p in give]}")
        ev = evaluate(me.roster, incoming=get, outgoing=give)
        report(ev, "if accepted")
        ok, why = clears_bar(ev, give)
        print("  VERDICT:", "ACCEPT (clears bar)" if ok else f"REJECT — {why}")
        if ok or not them:
            continue
        # counter search: swap exactly one player on either side
        cands = []
        offered = {p.playerId for p in get} | {p.playerId for p in give}
        for g in get:  # ask for a different player of theirs instead of g
            for r in them.roster:
                if r.playerId in offered: continue
                alt = [x for x in get if x is not g] + [r]
                e2 = evaluate(me.roster, alt, give)
                if clears_bar(e2, give)[0]:
                    cands.append((e2["delta"], f"swap their {g.name} -> {r.name}"))
        for w in give:  # offer a different player of mine instead of w
            for r in me.roster:
                if r.playerId in offered: continue
                alt = [x for x in give if x is not w] + [r]
                e2 = evaluate(me.roster, get, alt)
                if clears_bar(e2, alt)[0]:
                    cands.append((e2["delta"], f"swap my {w.name} -> {r.name}"))
        cands.sort(reverse=True)
        if cands:
            print("  COUNTER candidates (one-player swaps that clear the bar):")
            for d, desc in cands[:5]:
                print(f"    Δ {d:+}: {desc}")
        else:
            print("  no one-player swap clears the bar -> reject outright")
    if not found:
        print("no incoming trade proposals")


ROSTER_MAX = 16  # 9 starters + 7 bench; IR is extra


def respond_payload(action, tx_id, week, drop_items=()):
    """Exact packet ESPN's web client sends (kona main bundle, class `b.get()` + `acceptTrade` /
    `declineTrade`, captured 2026-09-13 from main-82d208d52efd2b467c49.js):
      decline: {isLeagueManager, teamId, type:"TRADE_DECLINE", memberId, scoringPeriodId,
                executionType:"EXECUTE", comment:null, relatedTransactionId}
               (+ items:[{playerId}] only when "dealbreaker" boxes are ticked — we never tick them)
      accept:  {isLeagueManager, teamId, type:"TRADE_ACCEPT", memberId, scoringPeriodId,
                executionType:"EXECUTE", relatedTransactionId}
               (+ items:[{playerId,type:"DROP",fromTeamId}] when the roster would overflow)
    Verified against the record ESPN stored for the owner's manual decline of 479e3b3b (TRADE_DECLINE,
    EXECUTE, EXECUTED, id ce8c8e47). POSTs to the same /transactions/ URL as every other move."""
    payload = {"isLeagueManager": False, "teamId": MY_TEAM_ID,
               "type": "TRADE_DECLINE" if action == "reject" else "TRADE_ACCEPT",
               "memberId": SWID, "scoringPeriodId": week, "executionType": "EXECUTE"}
    if drop_items:
        payload["items"] = list(drop_items)
    if action == "reject":
        payload["comment"] = None
    payload["relatedTransactionId"] = tx_id
    return payload


def cmd_respond(args):
    """Accept or reject a trade proposal sent TO us. Accept is gated by the rule-1/rule-3 bar
    (Δ ROS roster value > 0; core out ⇒ Δ ≥ 15 and ≥5 RB / ≥5 WR) unless --force REASON."""
    t = next((t for t in pending_trades() if t["id"] == args.id), None) or die(f"{args.id} is not a pending trade")
    if t.get("teamId") == MY_TEAM_ID:
        die(f"{args.id} is OUR proposal — use `withdraw`, not `respond`")
    if not any(i.get("toTeamId") == MY_TEAM_ID or i.get("fromTeamId") == MY_TEAM_ID for i in t["items"]):
        die(f"{args.id} does not involve team {MY_TEAM_ID}")
    lg = league()
    teams = {x.team_id: x for x in lg.teams}
    me = teams[MY_TEAM_ID]
    them = teams.get(t["teamId"])
    by_id = {p.playerId: p for p in me.roster + (them.roster if them else [])}
    get = [by_id[i["playerId"]] for i in t["items"] if i.get("toTeamId") == MY_TEAM_ID and i["playerId"] in by_id]
    give = [by_id[i["playerId"]] for i in t["items"] if i.get("fromTeamId") == MY_TEAM_ID and i["playerId"] in by_id]
    print(f"{args.action} {args.id} from team {t['teamId']} ({them.team_name.strip() if them else '?'})")
    print(f"  they give: {[p.name for p in get]}")
    print(f"  they want: {[p.name for p in give]}")
    drop_items = []
    if args.action == "accept":
        drops = [find_player(me.roster, n) for n in (args.drop or [])]
        ev = evaluate(me.roster, incoming=get, outgoing=give + drops)
        report(ev, "if accepted")
        enforce(ev, give + drops, args.force, "accept")
        # Autonomous-accept bar (2026-09-13): Δ ≥ +15 and 5 RB / 5 WR floors for EVERY accept,
        # core or not. --force is for manual use only.
        c = ev["counts"]
        if ev["delta"] < ACCEPT_MIN_GAIN and not args.force:
            die(f"accept refused: Δ {ev['delta']:+} < +{ACCEPT_MIN_GAIN} autonomous-accept bar")
        if (c.get("RB", 0) < MIN_RB or c.get("WR", 0) < MIN_WR) and not args.force:
            die(f"accept refused: roster after would be RB {c.get('RB',0)}/{MIN_RB}, WR {c.get('WR',0)}/{MIN_WR}")
        active = [p for p in me.roster if getattr(p, "lineupSlot", "") != "IR"]
        overflow = len(active) + len(get) - len(give) - len(drops) - ROSTER_MAX
        if overflow > 0:
            die(f"accepting overflows the roster by {overflow}; pass --drop with {overflow} more bench player(s)")
        _, starters_now = lineup_points(me.roster)
        for d in drops:
            if d.playerId in starters_now and not args.force:
                die(f"--drop {d.name} is a current starter (rule 2); use --force REASON if the replacement projects higher")
            drop_items.append({"playerId": d.playerId, "type": "DROP", "fromTeamId": MY_TEAM_ID})
    else:
        ev = evaluate(me.roster, incoming=get, outgoing=give)
        report(ev, "if accepted (rejecting)")
    resp = post(respond_payload(args.action, args.id, lg.current_week, drop_items), args.live)
    if args.live:
        kind = "TRADE_DECLINE" if args.action == "reject" else "TRADE_ACCEPT"
        rid = (resp or {}).get("id") if isinstance(resp, dict) else None
        print(f"\nESPN response id: {rid}")
        rec = verify_record(args.id, kind)
        if rec:
            print(f"VERIFIED {kind} id {rec.get('id')} status {rec.get('status')} "
                  f"(proposal {args.id}) — log this id + status in log.md and the brief")
        else:
            print(f"WARNING: no {kind} record for {args.id} in mTransactions2 yet — re-check before logging as done")


def verify_record(tx_id, kind):
    """Find the TRADE_ACCEPT / TRADE_DECLINE record ESPN stored for a proposal (mTransactions2)."""
    r = requests.get(READ_URL, params={"view": "mTransactions2"},
                     cookies={"espn_s2": ESPN_S2, "SWID": SWID}, timeout=30)
    r.raise_for_status()
    for tx in r.json().get("transactions", []):
        if tx.get("type") == kind and tx.get("relatedTransactionId") == tx_id:
            return tx
    return None


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("lineup"); a.add_argument("--moves", nargs="+", required=True, help='"Player Name:SLOT"')
    a.add_argument("--live", action="store_true"); a.set_defaults(fn=cmd_lineup)

    b = sub.add_parser("add"); b.add_argument("--add", required=True); b.add_argument("--drop"); b.add_argument("--waiver", action="store_true")
    b.add_argument("--force", metavar="REASON", help="override the Δ>0 bar (manual use only)")
    b.add_argument("--live", action="store_true"); b.set_defaults(fn=cmd_add)

    c = sub.add_parser("trade"); c.add_argument("--to-team", type=int, required=True)
    c.add_argument("--give", nargs="+", required=True); c.add_argument("--get", nargs="+", required=True)
    c.add_argument("--force", metavar="REASON", help="override the Δ / core / balance bar (manual use only)")
    c.add_argument("--live", action="store_true"); c.set_defaults(fn=cmd_trade)

    d = sub.add_parser("pending", help="list pending trade proposals"); d.set_defaults(fn=cmd_pending)

    e = sub.add_parser("withdraw"); e.add_argument("--id", required=True, help="ESPN transaction id of OUR pending proposal")
    e.add_argument("--live", action="store_true"); e.set_defaults(fn=cmd_withdraw)

    v = sub.add_parser("value", help="Δ ROS roster value for a hypothetical move (read-only)")
    v.add_argument("--add"); v.add_argument("--drop"); v.add_argument("--to-team", type=int)
    v.add_argument("--give", nargs="*"); v.add_argument("--get", nargs="*"); v.set_defaults(fn=cmd_value)

    i = sub.add_parser("incoming", help="score trade proposals sent to us + counter candidates"); i.set_defaults(fn=cmd_incoming)

    r = sub.add_parser("respond", help="accept/reject a trade proposal sent TO us")
    r.add_argument("--id", required=True); r.add_argument("--action", choices=["accept", "reject"], required=True)
    r.add_argument("--drop", nargs="*", help="(accept) bench players to drop if the roster would overflow")
    r.add_argument("--force", metavar="REASON", help="override the Δ / core / balance bar (manual use only)")
    r.add_argument("--live", action="store_true"); r.set_defaults(fn=cmd_respond)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
