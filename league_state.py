#!/usr/bin/env python3
"""
league_state.py — pull the current state of an ESPN fantasy football league
as clean JSON so a Claude routine can reason over it without a browser.

Usage:
    LEAGUE_ID=... TEAM_ID=... ESPN_S2="..." SWID="{...}" python3 league_state.py > state.json

Env vars (all required; see .env.example):
    ESPN_S2    the espn_s2 cookie from your browser
    SWID       the SWID cookie (keep the curly braces)
    LEAGUE_ID  your ESPN league id (the leagueId in the league URL)
    TEAM_ID    your team's id within that league
Optional:
    YEAR       season (default 2026)
"""
import json
import os
import socket
import sys

import requests
from datetime import datetime, timezone

from espn_api.football import League

# espn_api issues requests with no timeout; a stalled ESPN socket hung a run for 10+ min.
socket.setdefaulttimeout(30)

LEAGUE_ID = int(os.environ["LEAGUE_ID"])
YEAR = int(os.environ.get("YEAR", "2026"))
MY_TEAM_ID = int(os.environ["TEAM_ID"])
ESPN_S2 = os.environ.get("ESPN_S2")
SWID = os.environ.get("SWID")


def player_row(p):
    """Flatten a Player object into a plain dict. getattr with defaults
    protects us if the library changes a field name.

    These seven fields are the whole decision surface: ROS value is
    proj_season - pts_season, the lineup rules key off slot/pos, and rule 11
    picks search targets off injury. Nothing else ESPN returns is read by
    search_targets.py, transact.py or the dashboard, so it is dropped here to
    keep state.json (and the routine's context window) small. Dropped
    2026-09-15: pct_owned, bye — both unreferenced."""
    return {
        "name": getattr(p, "name", None),
        "pos": getattr(p, "position", None),
        "nfl_team": getattr(p, "proTeam", None),
        "slot": getattr(p, "lineupSlot", None),        # QB / RB / WR / BE / IR ...
        "injury": getattr(p, "injuryStatus", None),    # ACTIVE / QUESTIONABLE / OUT ...
        "proj_season": getattr(p, "projected_total_points", None),
        "pts_season": getattr(p, "total_points", None),
    }


def team_row(t):
    owners = getattr(t, "owners", [])
    # owners may be a list of dicts or strings depending on library version
    owner_names = []
    for o in owners:
        if isinstance(o, dict):
            owner_names.append(f"{o.get('firstName', '')} {o.get('lastName', '')}".strip())
        else:
            owner_names.append(str(o))
    return {
        "team_id": getattr(t, "team_id", None),
        "name": getattr(t, "team_name", None),
        "owners": owner_names,
        "record": f"{getattr(t, 'wins', 0)}-{getattr(t, 'losses', 0)}",
        "points_for": getattr(t, "points_for", None),
        "roster": [player_row(p) for p in getattr(t, "roster", [])],
    }


def main():
    if not (ESPN_S2 and SWID):
        sys.exit("Missing ESPN_S2 / SWID env vars — see docstring.")

    league = League(league_id=LEAGUE_ID, year=YEAR, espn_s2=ESPN_S2, swid=SWID)

    teams = [team_row(t) for t in league.teams]
    me = next((t for t in teams if t["team_id"] == MY_TEAM_ID), None)

    # Top free agents by position — these are the waiver candidates
    free_agents = {}
    for pos in ("QB", "RB", "WR", "TE", "D/ST", "K"):
        try:
            free_agents[pos] = [player_row(p) for p in league.free_agents(size=15, position=pos)]
        except Exception as e:  # position filter names occasionally differ
            free_agents[pos] = f"error: {e}"

    # This week's matchup for my team
    matchup = None
    try:
        for bs in league.box_scores():
            if bs.home_team.team_id == MY_TEAM_ID or bs.away_team.team_id == MY_TEAM_ID:
                matchup = {
                    "home": bs.home_team.team_name, "home_proj": getattr(bs, "home_projected", None),
                    "away": bs.away_team.team_name, "away_proj": getattr(bs, "away_projected", None),
                }
    except Exception as e:
        matchup = f"error: {e}"

    # Recent league activity (adds/drops/trades) so the bot knows what others are doing
    activity = []
    try:
        for a in league.recent_activity(size=25):
            activity.append({
                "date": getattr(a, "date", None),
                "actions": [
                    {"team": getattr(t, "team_name", str(t)), "action": act, "player": getattr(p, "name", str(p))}
                    for (t, act, p, *_) in a.actions
                ],
            })
    except Exception as e:
        activity = f"error: {e}"

    # Pending trade proposals / waiver claims — checked before the routine sends any trade so
    # it never duplicates a proposal a human (or a previous run) already submitted.
    pending = []
    try:
        url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{YEAR}"
               f"/segments/0/leagues/{LEAGUE_ID}")
        r = requests.get(url, params={"view": "mPendingTransactions"},
                         cookies={"espn_s2": ESPN_S2, "SWID": SWID}, timeout=30)
        r.raise_for_status()
        pid_to_name = {p.playerId: p.name for t in league.teams for p in t.roster}
        for tx in r.json().get("pendingTransactions", []):
            pending.append({
                "id": tx.get("id"), "type": tx.get("type"), "status": tx.get("status"),
                "by_team": tx.get("teamId"), "proposed": tx.get("proposedDate"),
                "expires": tx.get("expirationDate"),
                "items": [{"player": pid_to_name.get(i.get("playerId"), i.get("playerId")),
                           "from": i.get("fromTeamId"), "to": i.get("toTeamId"), "type": i.get("type")}
                          for i in tx.get("items", [])],
            })
    except Exception as e:
        pending = f"error: {e}"

    state = {
        "pulled_at": datetime.now(timezone.utc).isoformat(),
        "league": {"id": LEAGUE_ID, "year": YEAR, "week": getattr(league, "current_week", None),
                   "scoring": "PPR", "teams": len(teams)},
        "my_team": me,
        "matchup_this_week": matchup,
        "free_agents": free_agents,
        "other_teams": [t for t in teams if t["team_id"] != MY_TEAM_ID],
        "recent_activity": activity,
        "pending_transactions": pending,
    }
    # Compact separators, no indent: pure serialisation change (json.load is identical),
    # worth ~20% of the file. Read it with `python3 -m json.tool state.json` if eyeballing.
    json.dump(state, sys.stdout, separators=(",", ":"), default=str)


if __name__ == "__main__":
    main()
