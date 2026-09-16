#!/usr/bin/env python3
"""Fantasy manager dashboard. Stdlib only.

Serves index.html and a JSON view built from ../state.json, ../log.md, ../briefs/ and
../launchd.log. Routes are whitelisted: nothing else in the repo (notably .env) is ever
served. Intended to be run by launchd/systemd on 0.0.0.0:8787 (see README).
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    from zoneinfo import ZoneInfo
    PT = ZoneInfo("America/Los_Angeles")
except Exception:  # no tz database: fall back to PDT, which covers the season until November
    PT = timezone(timedelta(hours=-7))

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOST = "0.0.0.0"
PORT = int(os.environ.get("FANTASY_DASHBOARD_PORT", "8787"))
MY_TEAM = int(os.environ.get("TEAM_ID", "1"))

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def src(*parts):
    return os.path.join(ROOT, *parts)


def read_text(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def mtime_iso(p):
    try:
        return datetime.fromtimestamp(os.path.getmtime(p), timezone.utc).isoformat()
    except OSError:
        return None


def parse_ts(s):
    """log.md / launchd.log timestamps: '2026-09-13 08:20:00 PDT', '2026-09-13 0815', '2026-09-13 09:05'."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):?(\d{2})(?::(\d{2}))?", s.strip())
    if not m:
        return None
    y, mo, d, h, mi, sec = (int(g) if g else 0 for g in m.groups())
    try:
        return datetime(y, mo, d, h, mi, sec, tzinfo=PT)
    except ValueError:
        return None


def iso(dt):
    return dt.isoformat() if dt else None


def ms_iso(ms):
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


# ---------------------------------------------------------------- state.json

def load_state():
    raw = read_text(src("state.json"))
    if raw is None:
        return None, "state.json not found"
    try:
        return json.loads(raw), None
    except ValueError as e:
        return None, f"state.json is not valid JSON: {e}"


def team_names(state):
    names = {0: "Free agency"}
    my = state.get("my_team") or {}
    names[my.get("team_id", MY_TEAM)] = (my.get("name") or "My team").strip()
    for t in state.get("other_teams") or []:
        names[t.get("team_id")] = (t.get("name") or "").strip()
    return names


def player_id_names(obj, out=None):
    """Any dict in state.json carrying both an id and a name maps that id to the name."""
    out = {} if out is None else out
    if isinstance(obj, dict):
        pid = obj.get("id", obj.get("player_id"))
        if isinstance(pid, int) and isinstance(obj.get("name"), str):
            out[pid] = obj["name"]
        for v in obj.values():
            player_id_names(v, out)
    elif isinstance(obj, list):
        for v in obj:
            player_id_names(v, out)
    return out


NAME = r"((?:[A-Z][a-z]*\.\s?|[A-Z][a-z'-]+\s)?[A-Z][A-Za-z'-]+)"


def add_name_from_log(txn, log_lines):
    """state.json has no player ids, so a waiver ADD comes through as a bare ESPN id.
    log.md names it: either on the line carrying this transaction's id ("add Emari Demercado (")
    or as an "Add/Drop" pair next to the dropped player's surname ("Mooney/Douglas")."""
    short = (txn.get("id") or "")[:8]
    for line in log_lines:
        if short and short in line:
            m = re.search(r"\badd " + NAME + r"\s*\(", line)
            if m:
                return m.group(1)
    drop = next((i.get("player") for i in txn.get("items") or []
                 if i.get("type") == "DROP" and isinstance(i.get("player"), str)), None)
    if not drop:
        return None
    pair = re.compile(NAME + r"\s*/\s*(?:[A-Z]\.\s?)?" + re.escape(drop.split()[-1]) + r"\b")
    found = [m.group(1) for line in log_lines for m in pair.finditer(line)]
    return max(found, key=len) if found else None


def teams_view(state):
    """Every team with its owners, so the page can swap real names for "Team N"."""
    my = state.get("my_team") or {}
    out = [{"id": my.get("team_id", MY_TEAM), "name": (my.get("name") or "").strip(), "owners": my.get("owners") or []}]
    for t in state.get("other_teams") or []:
        out.append({"id": t.get("team_id"), "name": (t.get("name") or "").strip(), "owners": t.get("owners") or []})
    return out


DAY_MS = 24 * 3600 * 1000


def classify_pending(short, ttype, by_team, other_team, expires_ms, log_lines):
    """Needs my action: an incoming offer, a move log.md says was refused / needs --force, or an
    offer expiring within 24h. Everything else is waiting on ESPN (waivers) or the other manager."""
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    reasons = []
    if ttype == "TRADE_PROPOSAL" and by_team not in (None, MY_TEAM):
        reasons.append("incoming")
    if short and any(short in l and re.search(r"--force|refus", l, re.I) for l in log_lines):
        reasons.append("force")
    expired = isinstance(expires_ms, (int, float)) and expires_ms <= now_ms
    if isinstance(expires_ms, (int, float)) and 0 < expires_ms - now_ms < DAY_MS:
        reasons.append("expiring")
    if ttype == "WAIVER":
        waiting_on = "ESPN waiver processing"
    elif other_team is not None:
        waiting_on = f"team {other_team} to respond"
    else:
        waiting_on = "ESPN"
    return {"expires_ms": expires_ms, "reasons": reasons, "needs_action": bool(reasons),
            "expired": expired, "waiting_on": waiting_on}


def pending_view(state):
    names = team_names(state)
    pids = player_id_names(state)
    log_lines = (read_text(src("log.md")) or "").splitlines()
    rows = []
    for t in state.get("pending_transactions") or []:
        items = []
        for it in t.get("items") or []:
            p = it.get("player")
            if isinstance(p, int) and p not in pids:
                named = add_name_from_log(t, log_lines) if it.get("type") == "ADD" else None
                label = f"{named} (#{p}, name from log.md)" if named else f"player #{p}"
            else:
                label = pids.get(p, p)
            items.append({
                "type": it.get("type"),
                "player": label,
                "from": it.get("from"), "from_name": names.get(it.get("from")),
                "to": it.get("to"), "to_name": names.get(it.get("to")),
            })
        other = next((x for i in items for x in (i["from"], i["to"])
                      if isinstance(x, int) and x not in (0, MY_TEAM)), None)
        rows.append({
            "id": t.get("id"), "type": t.get("type"), "status": t.get("status"),
            "by_team": t.get("by_team"), "by_team_name": names.get(t.get("by_team")),
            "proposed": ms_iso(t.get("proposed")), "expires": ms_iso(t.get("expires")),
            "items": items,
            **classify_pending((t.get("id") or "")[:8], t.get("type"), t.get("by_team"), other,
                               t.get("expires"), log_lines),
        })
    return rows


# ---------------------------------------------------------------- log.md

def classify(kind, line):
    k = kind.upper()
    if k.startswith("RUN") or k == "SUMMARY":
        return "run"
    if k.startswith("RULE"):
        return "rule"
    if k.startswith("NOTE"):
        return "note"
    if k.startswith("INCOMING"):
        return "incoming"
    if "MANUAL" in k:
        return "manual"
    return "action"


def timeline():
    entries = []
    for n, line in enumerate((read_text(src("log.md")) or "").splitlines()):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(" | ")]
        dt = parse_ts(parts[0])
        rest = parts[1:] if dt else parts
        if rest and rest[0].lower().startswith("run:"):
            kind, details = "RUN", [rest[0][4:].strip()] + rest[1:]
        elif rest and re.match(r"^[A-Z]{3,}", rest[0]):
            kind, details = rest[0], rest[1:]
        else:
            kind, details = "SUMMARY", rest
        status = re.search(r"\bstatus ([A-Z_]+)", line)
        entries.append({
            "n": n, "ts": iso(dt), "raw_ts": parts[0] if dt else None,
            "kind": kind, "category": classify(kind, line), "details": details,
            "ids": re.findall(UUID, line), "status": status.group(1) if status else None,
            "live": "--live" in line,
        })
    # log.md is appended roughly in order, but manual entries get back-filled; sort by time.
    entries.sort(key=lambda e: (e["ts"] or "", e["n"]), reverse=True)
    return entries


def logged_after_snapshot(entries, state):
    """Moves log.md says are PENDING that the state.json snapshot predates."""
    try:
        pulled = datetime.fromisoformat(state.get("pulled_at"))
    except (TypeError, ValueError):
        return []
    known = {t.get("id") for t in state.get("pending_transactions") or []}
    log_lines = (read_text(src("log.md")) or "").splitlines()
    out = []
    for e in entries:
        if e["status"] != "PENDING" or not e["ts"] or not e["ids"]:
            continue
        if datetime.fromisoformat(e["ts"]) <= pulled or e["ids"][0] in known:
            continue
        short = e["ids"][0][:8]
        closed = any(
            o is not e and short in " ".join(o["details"]) and
            re.search(r"expired|withdr|CANCEL|accept|declin|EXECUTED", " ".join(o["details"]), re.I)
            for o in entries if (o["ts"] or "") >= e["ts"]
        )
        if not closed:
            line = " | ".join(e["details"])
            exp = re.search(r"\bexpires (\d{13})\b", line)
            to = re.search(r"\bto team (\d+)", line)
            ttype = "TRADE_PROPOSAL" if "TRADE_PROPOSAL" in e["kind"].upper() else e["kind"].split()[0]
            incoming = re.search(r"\bfrom team (\d+)", e["kind"] + " | " + line)
            by_team = (int(incoming.group(1)) if incoming else -1) if e["kind"].upper().startswith("INCOMING") else MY_TEAM
            out.append(dict(e, **classify_pending(short, ttype, by_team, int(to.group(1)) if to else None,
                                                  int(exp.group(1)) if exp else None, log_lines)))
    return out


# ---------------------------------------------------------------- launchd.log

MARK = re.compile(r"^=== (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (\w+) (.*?) ===\s*$")


def parse_tokens(line):
    body = line.split(":", 1)[1].strip()
    head, *segs = [s.strip() for s in body.split("|")]
    out = {"raw": line.strip(), "total": None, "est_cost": None, "turns": None, "searches": 0, "models": []}
    for kv in head.split():
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        v = v.lstrip("$")
        try:
            out[k] = float(v) if k == "est_cost" else int(v)
        except ValueError:
            out[k] = v
    for seg in segs:
        name, *kvs = seg.split()
        m = {"model": name}
        for kv in kvs:
            if "=" in kv:
                k, v = kv.split("=", 1)
                try:
                    m[k] = int(v)
                except ValueError:
                    m[k] = v
        out["searches"] += m.get("searches", 0) if isinstance(m.get("searches"), int) else 0
        out["models"].append(m)
    return out


def runs():
    result, cur = [], None

    def new():
        r = {"preflight": None, "start": None, "end": None, "exit": None, "auth_source": None,
             "claude_version": None, "tokens": None, "events": [], "output": []}
        result.append(r)
        return r

    for line in (read_text(src("launchd.log")) or "").splitlines():
        m = MARK.match(line)
        if m:
            ts, what = iso(parse_ts(m.group(1))), m.group(3)
            if what == "preflight":
                if cur is None or cur["preflight"] or cur["start"]:
                    cur = new()
                cur["preflight"] = ts
            elif what == "run start":
                if cur is None or cur["start"] or cur["end"]:
                    cur = new()
                cur["start"] = ts
            elif what.startswith("run end"):
                if cur is None or cur["end"]:
                    cur = new()
                cur["end"] = ts
                code = re.search(r"exit (\d+)", what)
                cur["exit"] = int(code.group(1)) if code else None
            else:
                if cur is None:
                    cur = new()
                cur["events"].append({"ts": ts, "text": what})
            continue
        if cur is None:
            continue
        if line.startswith("tokens:"):
            cur["tokens"] = parse_tokens(line)
        elif line.startswith("auth source:"):
            cur["auth_source"] = line.split(":", 1)[1].strip()
        elif line.startswith("claude --version:"):
            cur["claude_version"] = line.split(":", 1)[1].strip()
        elif line.startswith("which claude:"):
            pass
        elif line.strip() and len(cur["output"]) < 400:
            cur["output"].append(line)

    for r in result:
        r["started"] = r["start"] or r["preflight"]
        if r["exit"] is None and not r["start"] and any("AUTH EXPIRED" in e["text"] or "skipping" in e["text"]
                                                        for e in r["events"]):
            r["skipped"] = True
        note = next((l for l in r["output"] if "SessionEnd hook" not in l), None)
        r["note"] = note[:240] if note else None
        r["auth_expired"] = any("AUTH EXPIRED" in e["text"] for e in r["events"])
        if r["tokens"] and r["tokens"]["models"]:
            r["model"] = max(r["tokens"]["models"], key=lambda m: m.get("out", 0) if isinstance(m.get("out"), int) else 0)["model"]
        else:
            r["model"] = None
        r["output_tail"] = r["output"][-40:]
        del r["output"]
    return result


def health():
    rs = runs()
    last = rs[-1] if rs else None
    last_tokens = next((r for r in reversed(rs) if r["tokens"] and r["tokens"].get("total") is not None), None)
    last_auth = next((r["auth_source"] for r in reversed(rs) if r["auth_source"]), None)
    return {
        "log_mtime": mtime_iso(src("launchd.log")),
        "last_run": last,
        "last_run_with_tokens": last_tokens,
        "last_known_auth_source": last_auth,
        "recent": [{k: r.get(k) for k in ("started", "end", "exit", "model", "skipped", "auth_expired", "note")}
                   | {"tokens_total": (r["tokens"] or {}).get("total")} for r in rs[-20:]],
        "runs_total": len(rs),
    }


# ---------------------------------------------------------------- briefs

def brief_names():
    try:
        return sorted((f for f in os.listdir(src("briefs")) if f.endswith(".md")), reverse=True)
    except OSError:
        return []


def brief(name=None):
    names = brief_names()
    if not names:
        return None
    if name not in names:  # whitelist: only filenames actually in briefs/
        name = names[0]
    return {"name": name, "mtime": mtime_iso(src("briefs", name)), "text": read_text(src("briefs", name))}


def brief_ts(name):
    m = re.match(r"(\d{4}-\d{2}-\d{2})-(\d{4})", name)
    return parse_ts(f"{m.group(1)} {m.group(2)}") if m else None


# ---------------------------------------------------------------- thesis per move

# (kind, heading must match, heading must not match)
SECTION_KINDS = (
    ("lineup", re.compile(r"\blineup\b", re.I), re.compile(r"dry-run|packet", re.I)),
    ("waiver", re.compile(r"\b(adds?|waivers?)\b", re.I), re.compile(r"your pending|claims", re.I)),
    ("trade", re.compile(r"\btrades?\b", re.I), re.compile(r"incoming", re.I)),
)
# Blocks that list moves the run did NOT make ("Not sent", "Still pending, untouched", ...).
EXCLUDE = re.compile(r"^\W*(not sent|still pending|already pending|other options|pending, untouched)", re.I)
# Lines that start a new block even without a blank line before them.
LABEL = re.compile(r"^(\*\*[^*]{1,60}:\*\*|\W*(rationale|why|reason)\b|week \d+ proposal)", re.I)
FOLLOW = re.compile(r"^\W*(rationale|why|reason)\b", re.I)
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}")


def move_kind(entry):
    # "incoming" counts: an offer we judged is a decision too, and its Δ/VERDICT is the thesis.
    if entry["category"] not in ("action", "manual", "incoming"):
        return None
    k = re.sub(r"^INCOMING\s+", "", entry["kind"].upper())
    for prefix, kind in (("TRADE_PROPOSAL", "trade"), ("WAIVER", "waiver"), ("LINEUP", "lineup")):
        if k.startswith(prefix):
            return kind
    return None


def brief_sections(text):
    """Split a brief into ## sections, each a list of blocks: paragraphs, list items, tables."""
    sections, cur, block = [], None, None

    def close():
        nonlocal block
        if cur is not None and block:
            cur["blocks"].append(block)
        block = None

    for line in text.replace("\r", "").split("\n"):
        h = re.match(r"^#{1,6}\s+(.*)$", line)
        if h:
            close()
            heading = h.group(1).strip()
            kind = next((k for k, inc, exc in SECTION_KINDS if inc.search(heading) and not exc.search(heading)), None)
            cur = {"heading": heading, "kind": kind, "blocks": []}
            sections.append(cur)
            continue
        s = line.strip()
        if cur is None:
            continue
        if not s:
            close()
            continue
        is_table = s.startswith("|")
        new = block is None or is_table != block["table"] or (
            not is_table and (re.match(r"^([-*+]|\d+[.)])\s", s) or LABEL.match(s)))
        if new:
            close()
            block = {"table": is_table, "lines": []}
        block["lines"].append(line.rstrip())
    close()
    return sections


def best_block(section, score, need):
    """Highest-scoring block the run actually acted on, plus any Why/Rationale paragraphs after it."""
    best, excluded = None, False
    blocks = section["blocks"]
    for i, b in enumerate(blocks):
        head = b["lines"][0].strip()
        if EXCLUDE.match(head):
            excluded = True
        elif b["table"] or (LABEL.match(head) and not FOLLOW.match(head)):
            excluded = False
        if excluded:
            continue
        if b["table"]:
            header = b["lines"][:2] if len(b["lines"]) > 2 and TABLE_SEP.match(b["lines"][1]) else []
            for row in b["lines"][len(header):]:
                sc = score(row)
                if sc >= need and (best is None or sc > best[0]):
                    best = (sc, i, "\n".join(header + [row]))
        else:
            sc = score(" ".join(b["lines"]))
            if sc >= need and (best is None or sc > best[0]):
                best = (sc, i, "\n".join(b["lines"]))
    if not best:
        return None
    parts = [best[2]]
    for nb in blocks[best[1] + 1:]:
        if nb["table"] or not FOLLOW.match(nb["lines"][0].strip()):
            break
        parts.append("\n".join(nb["lines"]))
    return "\n\n".join(parts)


def move_surnames(details):
    names = []
    for d in details:
        m = re.match(r"^(give|get|add|drop)\s+(.*)$", d, re.I)
        if m:
            names += re.split(r"\s*\+\s*", re.sub(r"\s*\(.*", "", m.group(2)))
        for a, b in re.findall(r":\s*(.+?)\s*\([^)]*\)\s*in for\s+(.+?)\s*(?:\(|$)", d):
            names += [a, b]
    out = []
    for n in names:
        words = [w for w in re.findall(r"[A-Za-z'-]+", n) if w.lower() not in ("jr", "sr", "ii", "iii")]
        if words and len(words[-1]) >= 4:
            out.append(words[-1])
    return list(dict.fromkeys(out))


def find_thesis(entry, kind, parsed):
    """The brief written by the run that made the move (the first brief stamped at or after the move,
    allowing the run's brief name to predate its own log line by up to 90 min): ESPN id first, then
    player names. Later briefs within a day: id only. Otherwise the Δ breakdown from log.md."""
    ts = datetime.fromisoformat(entry["ts"]) if entry["ts"] else None
    near = sorted((p for p in parsed if ts and p[1] and p[1] >= ts - timedelta(minutes=90)), key=lambda p: p[1])
    short = entry["ids"][0][:8] if entry["ids"] else None
    surnames = move_surnames(entry["details"])
    for n, (name, bts, sections) in enumerate(near):
        if n and bts - ts > timedelta(hours=24):
            break
        tries = []
        if short:
            tries.append((lambda t: 1 if short in t else 0, 1))
        if surnames and n == 0:
            tries.append((lambda t: sum(1 for s in surnames if re.search(r"\b" + re.escape(s) + r"\b", t)),
                          min(2, len(surnames))))
        for score, need in tries:
            for sec in sections:
                if sec["kind"] != kind:
                    continue
                text = best_block(sec, score, need)
                if text:
                    return text, f"briefs/{name} § {sec['heading']}"
    deltas = [d for d in entry["details"] if "Δ" in d]
    if deltas:
        return "\n".join("- " + d for d in deltas), "log.md Δ (no matching brief text)"
    return None, None


def attach_theses(entries):
    parsed = []
    for name in brief_names():
        text = read_text(src("briefs", name))
        if text:
            parsed.append((name, brief_ts(name), brief_sections(text)))
    for e in entries:
        kind = move_kind(e)
        if not kind:
            continue
        d = re.search(r"Δ\s*([+\-−]?\d+(?:\.\d+)?)", " | ".join(e["details"]))
        e["delta"] = float(d.group(1).replace("−", "-")) if d else None
        e["thesis"], e["thesis_source"] = find_thesis(e, kind, parsed)
    return entries


# ---------------------------------------------------------------- trade count

TRADE_COUNT = re.compile(r"Week (\d+) (?:proposals?(?: count)?|trades?|cap)[:\s]*\**(\d+)\**\s*(?:/|of)\s*(\d+)", re.I)


def trade_count(week):
    """Proposals used this NFL week, from 'Week 2 proposal 1/3', 'Week 1 proposals: 3 of 3', 'Week 1 cap 3/3'."""
    if not isinstance(week, int):
        return None
    hits = [(m, "log.md") for m in TRADE_COUNT.finditer(read_text(src("log.md")) or "")]
    for name in brief_names():
        hits += [(m, f"briefs/{name}") for m in TRADE_COUNT.finditer(read_text(src("briefs", name)) or "")]
    mine = [(int(m.group(2)), int(m.group(3)), where) for m, where in hits if int(m.group(1)) == week]
    if not mine:
        return {"week": week, "used": 0, "cap": 3, "source": None}
    used, cap, where = max(mine, key=lambda h: h[0])
    return {"week": week, "used": used, "cap": cap, "source": where}


# ---------------------------------------------------------------- http

def data():
    state, err = load_state()
    entries = attach_theses(timeline())
    # Summary keys first, so `curl /api/data | head` shows them before the long timeline.
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "state_error": err}
    if state:
        out.update({
            "trade_count": trade_count((state.get("league") or {}).get("week")),
            "pending": pending_view(state),
            "logged_after_snapshot": logged_after_snapshot(entries, state),
            "teams": teams_view(state),
            "pulled_at": state.get("pulled_at"),
            "league": state.get("league") or {},
            "my_team": state.get("my_team") or {},
            "matchup": state.get("matchup_this_week"),
            "recent_activity": state.get("recent_activity") or [],
        })
    out.update({
        "state_mtime": mtime_iso(src("state.json")),
        "timeline": entries,
        "health": health(),
        "briefs": brief_names(),
        "brief": brief(),
    })
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "FantasyDashboard/1"

    def send(self, code, body, ctype):
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path in ("/", "/index.html"):
                html = read_text(os.path.join(HERE, "index.html"))
                if html is None:
                    return self.send(500, "index.html missing", "text/plain; charset=utf-8")
                return self.send(200, html, "text/html; charset=utf-8")
            if url.path == "/api/data":
                return self.send(200, json.dumps(data()), "application/json")
            if url.path == "/api/brief":
                name = (parse_qs(url.query).get("name") or [None])[0]
                return self.send(200, json.dumps(brief(name)), "application/json")
            if url.path == "/healthz":
                return self.send(200, "ok\n", "text/plain; charset=utf-8")
            return self.send(404, "not found\n", "text/plain; charset=utf-8")
        except Exception as e:  # keep serving; the error shows in the page and the log
            sys.stderr.write(f"error on {self.path}: {e!r}\n")
            return self.send(500, json.dumps({"error": repr(e)}), "application/json")

    do_HEAD = do_GET

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.log_date_time_string()} {self.client_address[0]} {fmt % args}\n")


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    sys.stderr.write(f"fantasy dashboard on http://{HOST}:{PORT} reading {ROOT}\n")
    sys.stderr.flush()
    httpd.serve_forever()


if __name__ == "__main__":
    main()
