#!/usr/bin/env python3
"""Print the only players the routine may web-search this run (CLAUDE.md rule 11), from state.json.

1. Players on my roster tagged QUESTIONABLE / DOUBTFUL / OUT, highest ROS first.
2. Top 5 free agents by ROS at my two weakest positions. Weakest = my best-N ROS at QB/RB/WR/TE
   (N = lineup slots 1/2/2/1) ranked against the other 15 teams; the two lowest ranks.
Capped at 10 in total. Injured roster players come first, then the two FA lists alternate.
"""
import itertools
import json

FLAGGED = {"QUESTIONABLE", "DOUBTFUL", "OUT"}
SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
FA_PER_POS = 5
MAX_SEARCHES = 10


def ros(p):
    return round((p.get("proj_season") or 0) - (p.get("pts_season") or 0), 2)


def top_n(roster, pos, n):
    return sum(sorted((ros(p) for p in roster if p["pos"] == pos), reverse=True)[:n])


def main():
    s = json.load(open("state.json"))
    mine = s["my_team"]["roster"]
    others = [t["roster"] for t in s["other_teams"]]

    ranks = {}  # 1 = best of 16
    for pos, n in SLOTS.items():
        me = top_n(mine, pos, n)
        ranks[pos] = 1 + sum(top_n(r, pos, n) > me for r in others)
    weakest = sorted(SLOTS, key=lambda pos: -ranks[pos])[:2]

    targets = [(p, f"roster, {p['injury']}")
               for p in sorted((p for p in mine if p["injury"] in FLAGGED), key=ros, reverse=True)]
    fa_lists = []
    for pos in weakest:
        fas = s["free_agents"].get(pos)
        fas = sorted(fas, key=ros, reverse=True)[:FA_PER_POS] if isinstance(fas, list) else []
        fa_lists.append([(p, f"FA, weak {pos}") for p in fas])
    for pair in itertools.zip_longest(*fa_lists):
        targets += [t for t in pair if t]
    targets = targets[:MAX_SEARCHES]

    print("Position ranks (1 = best of 16): " + ", ".join(f"{pos} {ranks[pos]}" for pos in SLOTS)
          + f" -> weakest: {', '.join(weakest)}")
    print(f"Search targets ({len(targets)}/{MAX_SEARCHES}) - search these and nobody else:")
    for i, (p, why) in enumerate(targets, 1):
        print(f"{i:2}. {p['name']} ({p['pos']}, {p['nfl_team']}) - {why}, ROS {ros(p)}")
    if not targets:
        print("   none - no web searches this run")


if __name__ == "__main__":
    main()
