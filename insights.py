import time

from . import handoff

WORKAROUND_WINDOW_S = 300
HISTORY_POINTS = 40
EVENT_LIMIT = 80
INCIDENT_LIMIT = 30
APPROVED = {"once", "session", "always"}

EVENT_COLS = ("id", "ts", "session_id", "tool", "action_class", "args_summary", "ok", "error", "gated",
              "approval", "flags")


def outcome(e):
    if e["action_class"] == "read":
        return "read"
    if e["error"] == "blocked":
        return "stopped"
    if not e["ok"]:
        return "error"
    approval = (e["approval"] or "").split(":")[-1]
    if e["gated"] and approval in APPROVED:
        return "approved"
    if e["gated"]:
        return "allowed"
    return "auto"


def _events(db, profile, since):
    rows = db.execute(f"SELECT {', '.join(EVENT_COLS)} FROM events WHERE profile=? AND ts>=? ORDER BY ts, id",
                      (profile, since)).fetchall()
    out = []
    for r in rows:
        e = dict(zip(EVENT_COLS, r))
        e["outcome"] = outcome(e)
        out.append(e)
    return out


def stats(events):
    acts = [e for e in events if e["outcome"] != "read"]
    count = lambda o: sum(1 for e in acts if e["outcome"] == o)
    auto = count("auto")
    return {
        "actions": len(acts),
        "reads": len(events) - len(acts),
        "auto": auto,
        "approved": count("approved") + count("allowed"),
        "stopped": count("stopped"),
        "errors": count("error"),
        "autonomy_pct": round(100 * auto / len(acts)) if acts else 0,
    }


def _incident(kind, e, title, detail, **extra):
    return {"kind": kind, "ts": e["ts"], "event_id": e.get("id"), "title": title, "detail": detail,
            "outcome": e.get("outcome"), "action_class": e.get("action_class"), **extra}


def incidents(events, turns=(), crossings=(), turn_starts=None):
    out = []
    starts = turn_starts or {}

    def new_turn_between(sid, a, b):
        return any(a < t <= b for t in starts.get(sid, ()))

    by_session = {}
    for e in events:
        by_session.setdefault(e["session_id"], []).append(e)
    for evs in by_session.values():
        for i, e in enumerate(evs):
            if e["outcome"] != "stopped":
                continue
            for n in evs[i + 1:]:
                if n["ts"] - e["ts"] > WORKAROUND_WINDOW_S or new_turn_between(e["session_id"], e["ts"], n["ts"]):
                    break
                if n["outcome"] == "read":
                    continue
                if n["tool"] != e["tool"] or n["args_summary"] != e["args_summary"]:
                    out.append(_incident("workaround", n, "Tried another way after being stopped",
                                         f"{e['args_summary'] or e['tool']}  →  {n['args_summary'] or n['tool']}",
                                         before_class=e["action_class"]))
                elif (e["approval"] or "").endswith("deny"):
                    out.append(_incident("retry", n, "Asked again after you said no",
                                         n["args_summary"] or n["tool"]))
                break
    for e in events:
        flags = e.get("flags") or ""
        if flags.startswith("taint:"):
            out.append(_incident("outside_content", e, "Risky action right after reading outside content",
                                 f"{e['args_summary'] or e['tool']}  (after {flags[6:]})"))
        if e["tool"] == "skill_manage":
            out.append(_incident("self_edit", e, "Agent tried to edit its own skills",
                                 e["args_summary"] or "skill_manage"))
        if (e["approval"] or "").endswith("deny"):
            out.append(_incident("denied", e, "You said no", e["args_summary"] or e["tool"]))
    for t in turns:
        out.append({"kind": "correction", "ts": t[0], "event_id": None, "title": "You corrected the agent",
                    "detail": (t[1] or "")[:120], "outcome": None, "action_class": None})
    for c in crossings:
        out.append({"kind": "autonomy" if c["up"] else "demoted", "ts": c["ts"], "event_id": None,
                    "title": f"{c['class']} {'reached autonomy' if c['up'] else 'lost autonomy'}",
                    "detail": f"{c['before']:.0f} → {c['after']:.0f} (threshold {c['threshold']:.0f}, {c['reason']})",
                    "outcome": None, "action_class": c["class"]})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:INCIDENT_LIMIT]


def crossings(db, engine, profile, since):
    rows = db.execute("SELECT ts, action_class, delta, score_after, reason FROM trust_events"
                      " WHERE profile=? AND ts>=? ORDER BY id", (profile, since)).fetchall()
    out = []
    for ts, cls, delta, after, reason in rows:
        t = engine.threshold(profile, cls)
        before = after - delta
        if (before < t) != (after < t):
            out.append({"ts": ts, "class": cls, "before": before, "after": after, "threshold": t,
                        "reason": reason, "up": after >= t})
    return out


def histories(db, profile, classes, start_score):
    out = {}
    for cls in classes:
        rows = db.execute("SELECT score_after FROM trust_events WHERE profile=? AND action_class=?"
                          " ORDER BY id DESC LIMIT ?", (profile, cls, HISTORY_POINTS)).fetchall()
        points = [round(r[0], 1) for r in reversed(rows)]
        out[cls] = ([start_score] if len(rows) < HISTORY_POINTS else []) + points
    return out


def handoff_summary(db, profile):
    s, others = handoff.find_open(db, profile, None, max_age_hours=72)
    if not s:
        return None
    done = db.execute("SELECT COUNT(*) FROM events WHERE session_id=? AND action_class!='read'",
                      (s["session_id"],)).fetchone()[0]
    return {"session_id": s["session_id"], "task": (s.get("original_task") or "")[:200],
            "cwd": s.get("cwd"), "last_activity": s.get("last_activity"), "actions": done, "others": others}


def build_state(db, engine, profile, current_turn=0, *, now=None, window_s=86400, taxonomy=None, config=None):
    now = now or time.time()
    since = now - window_s
    config = config or {}
    rows = engine.status(profile, current_turn)
    classes = [r["class"] for r in rows]
    if taxonomy is not None:
        classes = sorted(classes, key=lambda c: -taxonomy.rank.get(c, 0))
        order = {c: i for i, c in enumerate(classes)}
        rows = sorted(rows, key=lambda r: order[r["class"]])
    hist = histories(db, profile, classes, engine.config.get("start_score", 10))
    for r in rows:
        r["history"] = hist.get(r["class"], [])
    events = _events(db, profile, since)
    turns = db.execute("SELECT ts, user_excerpt FROM turns WHERE profile=? AND corrected=1 AND ts>=?",
                       (profile, since)).fetchall()
    starts = {}
    for sid, ts in db.execute("SELECT session_id, ts FROM turns WHERE profile=? AND ts>=?", (profile, since)):
        starts.setdefault(sid, []).append(ts)
    return {
        "generated_ts": now,
        "profile": profile,
        "window_s": window_s,
        "enabled": bool(config.get("enabled", True)),
        "taint_enabled": bool(config.get("taint_enabled", True)),
        "frozen": engine.is_frozen(profile),
        "classes": rows,
        "stats": stats(events),
        "events": list(reversed(events[-EVENT_LIMIT:])),
        "incidents": incidents(events, turns, crossings(db, engine, profile, since), starts),
        "handoff": handoff_summary(db, profile),
    }
